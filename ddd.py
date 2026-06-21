"""
企业微信审批管理CLI工具
支持查询审批、本地审批（同意/拒绝）、查看本地审批记录、企微消息通知
"""
import requests
import time
import click
import sqlite3
import os
from datetime import datetime, timedelta

# ===================== 企业微信配置 =====================
CORPID = "wwabc05e746c823209"
CORPSECRET = "gx0uRp7U_Nz4kZppRKQfdeNuK5pVFKrmX3s4G2jtTwo"
AGENTID = 1000002  # 改成你的自建应用 AgentId，在应用详情页顶部可以看到

GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
BATCH_APPROVAL_URL = "https://qyapi.weixin.qq.com/cgi-bin/oa/getapprovalinfo"
GET_APPROVAL_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/oa/getapprovaldetail"
SEND_MESSAGE_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

# 数据库配置
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approval_results.db")
# =======================================================

token_cache = {"access_token": None, "expire_time": 0}

STATUS_MAP = {
    "审批中": "1", "已通过": "2", "已驳回": "3", "已撤销": "4",
    "通过后撤销": "6", "已删除": "7", "已支付": "10"
}
REVERSE_STATUS_MAP = {v: k for k, v in STATUS_MAP.items()}


# ===================== 数据库操作 =====================
def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS approval_results (
            sp_no TEXT PRIMARY KEY,
            template_name TEXT,
            action TEXT NOT NULL,
            operator TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_approval_result(sp_no, template_name, action, operator, reason=""):
    """保存审批结果到本地数据库"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO approval_results (sp_no, template_name, action, operator, reason)
        VALUES (?, ?, ?, ?, ?)
    """, (sp_no, template_name, action, operator, reason))
    conn.commit()
    conn.close()


def get_local_approval_status(sp_no):
    """查询本地审批状态"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT action, operator, reason, created_at FROM approval_results WHERE sp_no = ?",
        (sp_no,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "action": row[0],
            "operator": row[1],
            "reason": row[2],
            "created_at": row[3]
        }
    return None


# ===================== 企微 API 操作 =====================
def send_approval_notification(sp_no, template_name, action, reason, applyer_userid):
    """发送审批结果通知给申请人"""
    token = get_access_token()
    
    action_cn = "已通过" if action == "approve" else "已驳回"
    reason_text = f"\n驳回原因：{reason}" if reason else ""
    
    content = (
        f"📋 审批结果通知\n\n"
        f"你的审批单「{template_name}」已被{action_cn}。\n"
        f"单号：{sp_no}{reason_text}\n\n"
        f"处理时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    body = {
        "touser": applyer_userid,
        "msgtype": "text",
        "agentid": AGENTID,
        "text": {
            "content": content
        }
    }
    
    try:
        resp = requests.post(
            f"{SEND_MESSAGE_URL}?access_token={token}",
            json=body,
            timeout=10
        )
        result = resp.json()
        if result.get("errcode") == 0:
            return True, "消息发送成功"
        else:
            return False, f"消息发送失败：{result.get('errmsg', '未知错误')}"
    except Exception as e:
        return False, f"消息发送异常：{e}"


def date_to_timestamp(date_str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"日期格式错误：{date_str}，支持 YYYY-MM-DD 或 YYYY-MM-DD HH:mm")


def auto_end_of_day(date_str):
    return date_str + " 23:59:59" if len(date_str) == 10 else date_str


def convert_status(status_str):
    if not status_str:
        return ""
    if status_str.isdigit():
        return status_str
    mapped = STATUS_MAP.get(status_str.strip())
    if mapped:
        return mapped
    raise click.BadParameter(f"未知状态：'{status_str}'，可用：{', '.join(STATUS_MAP.keys())}")


def get_access_token():
    now = int(time.time())
    if token_cache["access_token"] and token_cache["expire_time"] > now + 300:
        return token_cache["access_token"]
    resp = requests.get(GET_TOKEN_URL, params={"corpid": CORPID, "corpsecret": CORPSECRET}, timeout=10)
    data = resp.json()
    if data["errcode"] != 0:
        raise Exception(f"获取token失败：{data['errmsg']}")
    token_cache["access_token"] = data["access_token"]
    token_cache["expire_time"] = now + data["expires_in"]
    return token_cache["access_token"]


def batch_get_sp_no(starttime, endtime, template_id="", sp_status="", creator="", department=""):
    token = get_access_token()
    all_sp, cursor = [], ""
    while True:
        payload = {"starttime": starttime, "endtime": endtime, "new_cursor": cursor, "size": 100}
        filters = []
        if template_id:
            filters.append({"key": "template_id", "value": template_id})
        if creator:
            filters.append({"key": "creator", "value": creator})
        if department:
            filters.append({"key": "department", "value": department})
        if sp_status:
            filters.append({"key": "sp_status", "value": sp_status})
        if filters:
            payload["filters"] = filters

        resp = requests.post(f"{BATCH_APPROVAL_URL}?access_token={token}", json=payload, timeout=10)
        res = resp.json()
        if res["errcode"] != 0:
            raise Exception(f"获取审批列表失败：{res['errmsg']}")
        sp_list = res.get("sp_no_list", []) or res.get("data", {}).get("sp_no_list", [])
        all_sp.extend(sp_list)
        cursor = res.get("new_next_cursor", "") or res.get("data", {}).get("new_next_cursor", "")
        if not cursor:
            break
        time.sleep(0.2)
    return all_sp


def query_approval(sp_no):
    token = get_access_token()
    resp = requests.post(f"{GET_APPROVAL_DETAIL_URL}?access_token={token}",
                         json={"sp_no": sp_no}, timeout=10)
    result = resp.json()
    if result.get("errcode") != 0:
        raise Exception(f"查询失败：{result['errmsg']}")
    return result


def format_detail(sp_no, detail):
    info = detail.get("info", {})
    lines = [
        f"═══════════════════════════════════════",
        f"  审批单号：{sp_no}",
        f"  模板名称：{info.get('sp_name', '未知')}",
        f"  审批状态：{REVERSE_STATUS_MAP.get(str(info.get('sp_status', '')), '未知')}",
        f"  申请人：{info.get('applyer', {}).get('userid', '未知')}"
    ]
    apply_time = info.get("apply_time", 0)
    if apply_time > 1e12:
        apply_time //= 1000
    if apply_time:
        lines.append(f"  申请时间：{datetime.fromtimestamp(apply_time).strftime('%Y-%m-%d %H:%M:%S')}")

    records = info.get("sp_record", [])
    if records:
        lines.append("  ── 审批流程 ──")
        rec_map = {1: "审批中", 2: "已同意", 3: "已驳回", 4: "已转审",
                   11: "已退回", 12: "已加签", 13: "已同意并加签"}
        for i, rec in enumerate(records):
            attr = "会签" if rec.get("approverattr") == 2 else "或签"
            lines.append(f"  第{i+1}节点({attr}): {rec_map.get(rec.get('sp_status', 0), '未知')}")
            for d in rec.get("details", []):
                approver = d.get("approver", {}).get("userid", "未知")
                st = rec_map.get(d.get("sp_status", 0), "未知")
                speech = f" 意见: {d.get('speech')}" if d.get("speech") else ""
                lines.append(f"    ├ {approver} - {st}{speech}")

    contents = info.get("apply_data", {}).get("contents", [])
    if contents:
        lines.append("  ── 表单内容 ──")
        for c in contents:
            ctrl = c.get("control", "未知")
            title = c.get("title", [{}])[0].get("text", "未知") if c.get("title") else "未知"
            val = c.get("value", {})
            if ctrl in ("Text", "Textarea"):
                disp = val.get("text", "")
            elif ctrl == "Number":
                disp = val.get("new_number", "")
            elif ctrl == "Money":
                disp = val.get("new_money", "")
            elif ctrl == "Selector":
                opts = val.get("selector", {}).get("options", [])
                parts = []
                for o in opts:
                    vals = o.get("value", [])
                    texts = [v.get("text", "") for v in vals if v.get("text")]
                    parts.extend(texts)
                disp = ", ".join(parts) if parts else str(val)[:50]
            elif ctrl == "Date":
                date_val = val.get("date", {})
                ts = date_val.get("s_timestamp")
                if ts:
                    if int(ts) > 1e12: ts = int(ts) // 1000
                    disp = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
                else:
                    disp = str(val)[:50]
            elif ctrl == "File":
                disp = f"{len(val.get('files', []))}个附件"
            elif ctrl == "Table":
                children = val.get("children", [])
                if children:
                    table_lines = []
                    for ci, child in enumerate(children, 1):
                        child_list = child.get("list", [])
                        for cc in child_list:
                            ctrl2 = cc.get("control", "")
                            title2 = cc.get("title", [{}])[0].get("text", "") if cc.get("title") else ""
                            val2 = cc.get("value", {})
                            if ctrl2 in ("Text", "Textarea"):
                                disp2 = val2.get("text", "")
                            elif ctrl2 == "Selector":
                                opts2 = val2.get("selector", {}).get("options", [])
                                parts2 = []
                                for o in opts2:
                                    for v in o.get("value", []):
                                        if v.get("text"): parts2.append(v["text"])
                                disp2 = ", ".join(parts2)
                            elif ctrl2 == "Date":
                                ts2 = val2.get("date", {}).get("s_timestamp", 0)
                                if ts2:
                                    if int(ts2) > 1e12: ts2 = int(ts2) // 1000
                                    disp2 = datetime.fromtimestamp(int(ts2)).strftime("%m-%d %H:%M")
                                else:
                                    disp2 = ""
                            elif ctrl2 == "Money":
                                disp2 = val2.get("new_money", "")
                            elif ctrl2 == "Number":
                                disp2 = val2.get("new_number", "")
                            else:
                                disp2 = str(val2)[:30]
                            if title2 and disp2:
                                table_lines.append(f"    ├ {title2}: {disp2}")
                    if table_lines:
                        disp = f"{len(children)}行明细"
                        lines.append(f"  {title}: {disp}")
                        lines.extend(table_lines)
                        continue
                    else:
                        disp = f"{len(children)}行明细"
                else:
                    disp = "0行明细"
            elif ctrl == "Location":
                disp = val.get("location", {}).get("title", "")
            elif ctrl == "Contact":
                members = val.get("members", [])
                if members:
                    names = [m.get("name", m.get("userid", "")) for m in members]
                    disp = ", ".join(names)
                else:
                    disp = str(val)[:50]
            elif ctrl == "RelatedApproval":
                disp = ", ".join([r.get("sp_no", "") for r in val.get("related_approval", [])])
            elif ctrl == "DateRange":
                dr = val.get("date_range", {})
                begin_ts = dr.get("new_begin", 0)
                end_ts = dr.get("new_end", 0)
                if begin_ts > 1e12: begin_ts //= 1000
                if end_ts > 1e12: end_ts //= 1000
                begin_str = datetime.fromtimestamp(begin_ts).strftime("%Y-%m-%d %H:%M") if begin_ts else ""
                end_str = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M") if end_ts else ""
                disp = f"{begin_str} ~ {end_str}"
            elif ctrl == "Attendance":
                dr = val.get("attendance", {}).get("date_range", {})
                begin_ts = dr.get("new_begin", 0)
                end_ts = dr.get("new_end", 0)
                if begin_ts > 1e12: begin_ts //= 1000
                if end_ts > 1e12: end_ts //= 1000
                begin_str = datetime.fromtimestamp(begin_ts).strftime("%Y-%m-%d %H:%M") if begin_ts else ""
                end_str = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M") if end_ts else ""
                disp = f"{begin_str} ~ {end_str}"
            elif ctrl == "Vacation":
                vac = val.get("vacation", {})
                sel = vac.get("selector", {})
                opts = sel.get("options", [])
                vac_type = opts[0].get("value", [{}])[0].get("text", "") if opts else ""
                att = vac.get("attendance", {})
                dr = att.get("date_range", {})
                begin_ts = dr.get("new_begin", 0)
                end_ts = dr.get("new_end", 0)
                if begin_ts > 1e12: begin_ts //= 1000
                if end_ts > 1e12: end_ts //= 1000
                begin_str = datetime.fromtimestamp(begin_ts).strftime("%Y-%m-%d %H:%M") if begin_ts else ""
                end_str = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M") if end_ts else ""
                disp = f"{vac_type} | {begin_str} ~ {end_str}" if vac_type else f"{begin_str} ~ {end_str}"
            else:
                disp = str(val)[:50]
            lines.append(f"  {title}: {disp}")
    lines.append("═══════════════════════════════════════")
    return "\n".join(lines)


# ===================== CLI 命令 =====================
@click.group()
def cli():
    """企业微信审批管理工具 - 查询、详情、待处理清单、本地审批、消息通知"""
    init_db()


@cli.command()
@click.option("--start", "-s", required=True, help="开始日期（YYYY-MM-DD 或 YYYY-MM-DD HH:mm）")
@click.option("--end", "-e", required=True, help="结束日期（仅日期自动补 23:59:59）")
@click.option("--template", "-t", default="", help="模板ID")
@click.option("--status", "-st", default="", help="状态：审批中/已通过/已驳回/已撤销，也支持数字")
@click.option("--creator", "-c", default="", help="申请人userid")
@click.option("--department", "-d", default="", help="部门id")
@click.option("--no-status", is_flag=True, help="不显示每条单据状态（加快速度）")
def list(start, end, template, status, creator, department, no_status):
    """列出审批单号，默认显示状态"""
    try:
        end_final = auto_end_of_day(end)
        status_code = convert_status(status)
        start_ts = date_to_timestamp(start)
        end_ts = date_to_timestamp(end_final)
        if (end_ts - start_ts) / 86400 > 31:
            click.echo("❌ 时间跨度不能超过31天", err=True)
            return

        status_text = REVERSE_STATUS_MAP.get(status_code, status or "所有状态")
        click.echo(f"📅 查询范围：{start} ~ {end}")
        click.echo(f"📌 筛选状态：{status_text}")
        if creator:
            click.echo(f"👤 申请人：{creator}")
        if department:
            click.echo(f"🏢 部门：{department}")

        sp_list = batch_get_sp_no(start_ts, end_ts, template, status_code, creator, department)
        if not sp_list:
            click.echo("\n🎉 没有符合条件的审批单！")
        else:
            if no_status:
                click.echo(f"\n共找到 {len(sp_list)} 条审批单：")
                for i, sp in enumerate(sp_list, 1):
                    click.echo(f"  {i:3d}. {sp}")
            else:
                click.echo(f"\n共找到 {len(sp_list)} 条审批单，正在获取状态...")
                for i, sp in enumerate(sp_list, 1):
                    try:
                        detail = query_approval(sp)
                        sp_status = detail.get("info", {}).get("sp_status", "")
                        status_cn = REVERSE_STATUS_MAP.get(str(sp_status), f"未知({sp_status})")
                        local = get_local_approval_status(sp)
                        local_tag = ""
                        if local:
                            local_tag = f" [本地已{'同意' if local['action'] == 'approve' else '驳回'}]"
                    except:
                        status_cn = "获取失败"
                        local_tag = ""
                    click.echo(f"  {i:3d}. {sp}  [{status_cn}]{local_tag}")
                    time.sleep(0.1)

        click.echo("\n💡 下一步操作：")
        click.echo("  查看详情：  python ddd.py detail -n <单号>")
        click.echo("  同意审批：  python ddd.py approve -n <单号>")
        click.echo("  驳回审批：  python ddd.py reject -n <单号>")
        click.echo("  待处理：    python ddd.py process")
    except Exception as e:
        click.echo(f"❌ 错误：{e}", err=True)


@cli.command()
@click.option("--start", "-s", default="", help="开始日期")
@click.option("--end", "-e", default="", help="结束日期（自动补全）")
@click.option("--template", "-t", default="", help="模板ID")
@click.option("--status", "-st", default="", help="状态筛选（汉字或数字）")
@click.option("--sp-no", "-n", default="", help="指定单号")
def detail(start, end, template, status, sp_no):
    """查看审批详情"""
    try:
        if sp_no:
            click.echo(f"🔍 查询单号：{sp_no}")
            data = query_approval(sp_no)
            click.echo(format_detail(sp_no, data))
            
            local = get_local_approval_status(sp_no)
            if local:
                action_cn = "同意" if local["action"] == "approve" else "驳回"
                click.echo(f"\n📋 本地审批记录：已{action_cn} | 操作人：{local['operator']} | 时间：{local['created_at']}")
                if local["reason"]:
                    click.echo(f"   原因：{local['reason']}")
            
            info = data.get("info", {})
            if info.get("sp_status") == 1:
                click.echo("\n💡 该单据审批中，可在CLI中操作：")
                click.echo("  同意：python ddd.py approve -n <单号>")
                click.echo("  驳回：python ddd.py reject -n <单号> -r \"原因\"")
            else:
                click.echo("\n💡 该单据已处理，无需操作。")
        else:
            if not start or not end:
                click.echo("❌ 需要 --start 和 --end", err=True)
                return
            end_final = auto_end_of_day(end)
            status_code = convert_status(status)
            start_ts = date_to_timestamp(start)
            end_ts = date_to_timestamp(end_final)
            if (end_ts - start_ts) / 86400 > 31:
                click.echo("❌ 超过31天", err=True)
                return
            status_text = REVERSE_STATUS_MAP.get(status_code, status or "所有状态")
            click.echo(f"📅 查询范围：{start} ~ {end}")
            click.echo(f"📌 筛选状态：{status_text}")
            sp_list = batch_get_sp_no(start_ts, end_ts, template, status_code)
            if not sp_list:
                click.echo("🎉 没有符合条件的单据！")
            else:
                click.echo(f"\n共找到 {len(sp_list)} 条，逐一查看详情：")
                for idx, sp in enumerate(sp_list, 1):
                    click.echo(f"\n--- 第 {idx}/{len(sp_list)} 条 ---")
                    click.echo(format_detail(sp, query_approval(sp)))
                    time.sleep(0.1)
                click.echo("\n💡 下一步操作：")
                click.echo("  同意：python ddd.py approve -n <单号>")
                click.echo("  驳回：python ddd.py reject -n <单号> -r \"原因\"")
    except Exception as e:
        click.echo(f"❌ 错误：{e}", err=True)


@cli.command()
@click.option("--start", "-s", default="", help="开始日期（YYYY-MM-DD）")
@click.option("--end", "-e", default="", help="结束日期（默认今天，自动补全）")
@click.option("--days", "-d", type=int, default=0, help="查询最近多少天（如 3）")
@click.option("--sp-no", "-n", default="", help="指定单号")
def process(start, end, days, sp_no):
    """查看待处理审批并给出操作指引"""
    try:
        if days > 0:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            range_desc = f"最近{days}天"
        else:
            if not end:
                end = datetime.now().strftime("%Y-%m-%d")
            if not start:
                start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                range_desc = "最近7天"
            else:
                range_desc = f"{start} ~ {end}"
        end_api = auto_end_of_day(end)

        if sp_no:
            click.echo(f"🔍 查询单号：{sp_no}")
            data = query_approval(sp_no)
            click.echo(format_detail(sp_no, data))
            
            local = get_local_approval_status(sp_no)
            if local:
                action_cn = "同意" if local["action"] == "approve" else "驳回"
                click.echo(f"\n📋 本地审批记录：已{action_cn} | 操作人：{local['operator']} | 时间：{local['created_at']}")
            
            info = data.get("info", {})
            if info.get("sp_status") == 1:
                click.echo("\n💡 该单据审批中，可在CLI中操作：")
                click.echo("  同意：python ddd.py approve -n <单号>")
                click.echo("  驳回：python ddd.py reject -n <单号> -r \"原因\"")
            else:
                click.echo("\n💡 该单据已处理，无需操作。")
        else:
            start_ts = date_to_timestamp(start)
            end_ts = date_to_timestamp(end_api)
            if (end_ts - start_ts) / 86400 > 31:
                click.echo("❌ 时间跨度不能超过31天", err=True)
                return
            click.echo(f"📅 查询范围：{range_desc}（{start} ~ {end}）")
            sp_list = batch_get_sp_no(start_ts, end_ts, sp_status="1")
            if not sp_list:
                click.echo("🎉 没有待审批的单据！")
                click.echo("💡 提示：可扩大范围重试，例如 python ddd.py process --days 14")
                return
            pending = []
            for sp in sp_list:
                try:
                    info = query_approval(sp)["info"]
                    local = get_local_approval_status(sp)
                    local_tag = ""
                    if local:
                        local_tag = f" [本地已{'同意' if local['action'] == 'approve' else '驳回'}]"
                    pending.append((sp, info.get("sp_name", ""), info.get("applyer", {}).get("userid", ""),
                                    info.get("apply_time", 0), local_tag))
                except:
                    pass
            click.echo(f"待审批单据 {len(pending)} 个：")
            for i, (sp, name, applyer, ts, tag) in enumerate(pending, 1):
                if ts > 1e12:
                    ts //= 1000
                t = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""
                click.echo(f"  [{i}] {sp} | {name} | {applyer} | {t}{tag}")
            click.echo("\n💡 快捷操作：")
            click.echo("  同意：python ddd.py approve -n <单号>")
            click.echo("  驳回：python ddd.py reject -n <单号> -r \"原因\"")
            click.echo("  详情：python ddd.py detail -n <单号>")
    except Exception as e:
        click.echo(f"❌ 错误：{e}", err=True)


@cli.command()
@click.option("--sp-no", "-n", required=True, help="审批单号")
@click.option("--confirm", "-y", is_flag=True, help="跳过确认，直接同意")
def approve(sp_no, confirm):
    """同意审批单（本地审批 + 企微通知）"""
    try:
        init_db()

        detail = query_approval(sp_no)
        info = detail.get("info", {})
        sp_name = info.get("sp_name", "未知")
        applyer = info.get("applyer", {}).get("userid", "未知")
        status = REVERSE_STATUS_MAP.get(str(info.get("sp_status", "")), "未知")

        click.echo(f"\n{'='*40}")
        click.echo(f"  审批单号：{sp_no}")
        click.echo(f"  模板名称：{sp_name}")
        click.echo(f"  申请人：{applyer}")
        click.echo(f"  当前状态：{status}")
        click.echo(f"{'='*40}")

        if str(info.get("sp_status")) != "1":
            click.echo(f"\n❌ 该审批单当前状态为「{status}」，无需审批")
            return

        if not confirm:
            click.confirm("\n⚠️  确认同意该审批申请？", abort=True)

        save_approval_result(sp_no, sp_name, "approve", "CLI管理员", "")

        click.echo(f"\n✅ 审批单 {sp_no} 已同意！（本地记录）")
        
        # 发送通知
        click.echo(f"\n📨 正在发送通知...")
        success, msg = send_approval_notification(sp_no, sp_name, "approve", "", applyer)
        if success:
            click.echo(f"✅ 已通知申请人 {applyer}")
        else:
            click.echo(f"⚠️  {msg}")

    except click.Abort:
        click.echo("\n已取消操作")
    except Exception as e:
        click.echo(f"\n❌ 错误：{e}", err=True)


@cli.command()
@click.option("--sp-no", "-n", required=True, help="审批单号")
@click.option("--reason", "-r", default="", help="驳回原因")
@click.option("--confirm", "-y", is_flag=True, help="跳过确认，直接驳回")
def reject(sp_no, reason, confirm):
    """驳回审批单（本地审批 + 企微通知）"""
    try:
        init_db()

        detail = query_approval(sp_no)
        info = detail.get("info", {})
        sp_name = info.get("sp_name", "未知")
        applyer = info.get("applyer", {}).get("userid", "未知")
        status = REVERSE_STATUS_MAP.get(str(info.get("sp_status", "")), "未知")

        click.echo(f"\n{'='*40}")
        click.echo(f"  审批单号：{sp_no}")
        click.echo(f"  模板名称：{sp_name}")
        click.echo(f"  申请人：{applyer}")
        click.echo(f"  当前状态：{status}")
        click.echo(f"{'='*40}")

        if str(info.get("sp_status")) != "1":
            click.echo(f"\n❌ 该审批单当前状态为「{status}」，无需审批")
            return

        if not reason:
            reason = click.prompt("请输入驳回原因", default="无")

        if not confirm:
            click.echo(f"\n驳回原因：{reason}")
            click.confirm("\n⚠️  确认驳回该审批申请？", abort=True)

        save_approval_result(sp_no, sp_name, "reject", "CLI管理员", reason)

        click.echo(f"\n✅ 审批单 {sp_no} 已驳回！（本地记录）")
        
        # 发送通知
        click.echo(f"\n📨 正在发送通知...")
        success, msg = send_approval_notification(sp_no, sp_name, "reject", reason, applyer)
        if success:
            click.echo(f"✅ 已通知申请人 {applyer}")
        else:
            click.echo(f"⚠️  {msg}")

    except click.Abort:
        click.echo("\n已取消操作")
    except Exception as e:
        click.echo(f"\n❌ 错误：{e}", err=True)


@cli.command()
@click.option("--sp-no", "-n", default="", help="审批单号，不填则查看全部")
def local(sp_no):
    """查看本地审批记录"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if sp_no:
        cursor.execute(
            "SELECT sp_no, template_name, action, operator, reason, created_at FROM approval_results WHERE sp_no = ?",
            (sp_no,)
        )
    else:
        cursor.execute(
            "SELECT sp_no, template_name, action, operator, reason, created_at FROM approval_results ORDER BY created_at DESC LIMIT 50"
        )

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        click.echo("📭 暂无本地审批记录")
        return

    click.echo(f"\n{'='*60}")
    click.echo(f"  本地审批记录（共 {len(rows)} 条）")
    click.echo(f"{'='*60}")
    for row in rows:
        action_cn = "✅ 同意" if row[2] == "approve" else "❌ 驳回"
        click.echo(f"  单号: {row[0]}")
        click.echo(f"  模板: {row[1]}")
        click.echo(f"  操作: {action_cn}")
        click.echo(f"  操作人: {row[3]}")
        click.echo(f"  原因: {row[4] or '无'}")
        click.echo(f"  时间: {row[5]}")
        click.echo(f"  {'-'*40}")


if __name__ == "__main__":
    cli()