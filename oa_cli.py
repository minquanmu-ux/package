import requests
import time
import json
import click
from datetime import datetime

# ===================== 企业微信配置 =====================
CORPID = "ww071488e66ad1ab9e"
CORPSECRET = "3XhIOhvPevqUTZETi5OjPRtSH3e-AnPIKEKw0oq2wnQ"

# 接口地址
GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
BATCH_APPROVAL_URL = "https://qyapi.weixin.qq.com/cgi-bin/oa/getapprovalinfo"
GET_APPROVAL_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/oa/getapprovaldetail"
# =======================================================

# Token内存缓存，过期前5分钟自动重刷
token_cache = {
    "access_token": None,
    "expire_time": 0
}

def date_to_timestamp(date_str: str) -> int:
    """YYYY-MM-DD 日期转Unix时间戳(秒)"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp())

def get_access_token() -> str:
    """获取/复用access_token"""
    now = int(time.time())
    click.echo(f"【调试】当前时间戳: {now}, 缓存信息: {token_cache}")

    # 缓存有效，直接复用
    if token_cache["access_token"] and token_cache["expire_time"] > now + 300:
        click.echo("【调试】复用缓存token，不再调用gettoken接口")
        return token_cache["access_token"]

    click.echo("【调试】缓存失效/无缓存，重新请求获取新token")
    params = {"corpid": CORPID, "corpsecret": CORPSECRET}
    resp = requests.get(GET_TOKEN_URL, params=params, timeout=10)
    data = resp.json()
    click.echo(f"【调试】gettoken接口返回: {json.dumps(data, ensure_ascii=False)}")

    if data["errcode"] != 0:
        raise Exception(f"获取token失败：{data['errcode']} {data['errmsg']}")

    token_cache["access_token"] = data["access_token"]
    token_cache["expire_time"] = now + data["expires_in"]
    click.echo(f"【调试】新token过期时间戳: {token_cache['expire_time']}")
    return token_cache["access_token"]

def batch_get_sp_no(starttime: int, endtime: int, template_id: str = "") -> list:
    """批量拉取审批单号，兼容新旧返回格式"""
    token = get_access_token()
    all_sp_no = []
    new_cursor = ""
    page = 1

    while True:
        payload = {
            "starttime": starttime,
            "endtime": endtime,
            "template_id": template_id,
            "new_cursor": new_cursor,
            "size": 100
        }
        click.echo(f"\n【调试】第{page}页请求参数: {json.dumps(payload, ensure_ascii=False)}")
        url = f"{BATCH_APPROVAL_URL}?access_token={token}"
        resp = requests.post(url, json=payload, timeout=10)
        res = resp.json()
        click.echo(f"【调试】第{page}页接口原始返回: {json.dumps(res, ensure_ascii=False)}")

        if res["errcode"] != 0:
            raise Exception(f"批量获取审批记录失败：{res['errcode']} {res['errmsg']}")

        # 兼容两种返回结构：sp_no_list在根节点 / 在data子节点
        sp_no_list = res.get("sp_no_list", [])
        if not sp_no_list and "data" in res:
            sp_no_list = res["data"].get("sp_no_list", [])

        click.echo(f"【调试】第{page}页本次返回单据数量: {len(sp_no_list)}")
        for sp_no in sp_no_list:
            all_sp_no.append(sp_no)
            click.echo(f"已获取审批单号：{sp_no}")

        # 兼容分页游标
        new_next_cursor = res.get("new_next_cursor", "")
        if not new_next_cursor and "data" in res:
            new_next_cursor = res["data"].get("new_next_cursor", "")

        click.echo(f"【调试】下一页游标: {new_next_cursor}")
        if not new_next_cursor:
            click.echo("【调试】无下一页，分页结束")
            break
        new_cursor = new_next_cursor
        page += 1
        time.sleep(0.2)
    return all_sp_no

def query_approval(sp_no: str):
    """查询单条审批详情（修复官方接口地址）"""
    token = get_access_token()
    url = f"{GET_APPROVAL_DETAIL_URL}?access_token={token}"
    payload = {"sp_no": sp_no}
    click.echo(f"\n【调试】查询单据 {sp_no}，请求参数：{json.dumps(payload)}")
    resp = requests.post(url, json=payload, timeout=10)
    click.echo(f"【调试】单据{sp_no}接口原始响应文本：{repr(resp.text)}")
    try:
        result = resp.json()
        click.echo(f"【调试】单据{sp_no}详情完整返回：{json.dumps(result, ensure_ascii=False)}")
        return result
    except json.JSONDecodeError as e:
        raise Exception(f"单据{sp_no}JSON解析失败：{str(e)}，原始返回：{repr(resp.text)}")

@click.command()
@click.option("--start", "-s", required=True, help="起始日期，格式 YYYY-MM-DD，例：2026-06-13")
@click.option("--end", "-e", required=True, help="结束日期，格式 YYYY-MM-DD，例：2026-06-17")
@click.option("--template", "-t", default="", help="审批模板ID，不传查全部模板")
@click.option("--detail/--no-detail", default=False, help="是否查询单据完整详情，默认关闭")
def main(start, end, template, detail):
    """企业微信审批批量查询CLI工具
    示例：
    oa_cli.exe -s 2026-06-13 -e 2026-06-17
    oa_cli.exe -s 2026-06-13 -e 2026-06-17 --detail
    """
    try:
        click.echo("开始自动申请access_token...")
        token = get_access_token()
        click.echo("后端内部token获取成功，内存缓存生效（不对外输出前端）\n")

        start_ts = date_to_timestamp(start)
        end_ts = date_to_timestamp(end)

        # 校验接口31天限制
        day_diff = (end_ts - start_ts) / (24 * 3600)
        if day_diff > 31:
            click.echo(f"错误：时间跨度 {day_diff:.1f} 天，超过接口最大31天限制，请缩小日期范围", err=True)
            return

        click.echo(f"查询时间范围：{start} ~ {end}")
        click.echo(f"起始时间戳：{start_ts}  结束时间戳：{end_ts}\n")

        sp_no_list = batch_get_sp_no(start_ts, end_ts, template)
        click.echo(f"\n批量拉取完成，单据总数：{len(sp_no_list)}")
        # ========== 核心修复：f-string拼接，不再多参数传入click.echo ==========
        click.echo(f"全部审批单号集合：{sp_no_list}")

        if len(sp_no_list) == 0:
            click.echo("【警告】当前时间段未查询到任何审批单据！请检查：日期范围/审批API权限/应用可见范围")
            return

        if detail:
            click.echo("\n===== 批量查询所有审批单完整详情 =====")
            for sp in sp_no_list:
                try:
                    detail_data = query_approval(sp)
                    click.echo(f"单号 {sp} 详情查询完成")
                except Exception as e:
                    click.echo(f"【异常】单号 {sp} 查询失败：{str(e)}", err=True)
                time.sleep(0.1)

    except Exception as e:
        click.echo(f"服务端接口异常：{str(e)}", err=True)

if __name__ == "__main__":
    main()