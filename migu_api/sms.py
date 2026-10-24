# -*- coding: utf-8 -*-
"""
sms.py —— 从测试环境日志实时获取短信验证码

原理：
    咪咕测试环境的短信服务会把每条验证码短信以 DEBUG 级别打进
    https://union-passport.migu.cn/app-7001.log ，格式固定为：

        ... || receiver = 18702103621, message = 验证码613962，仅用于咪咕账号登录，...

    所以"获取验证码"不需要真手机，只要：
        1. GET 这个日志文件（可能很大，只读尾部即可）
        2. 用正则按手机号过滤出该号码的验证码记录
        3. 取时间戳最新的一条（验证码有效期 5 分钟，太旧的直接放弃）

注意事项：
    - 测试环境是自签证书（CN=localhost），requests 必须传 verify=False
    - 日志里有所有人的手机号，本模块只精确匹配传入的那个号码
"""
import re
import time
from datetime import datetime

import requests
import urllib3

from .config import env

# 关闭"自签证书"产生的 InsecureRequestWarning 刷屏（仅测试环境用）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _require_log_url() -> str:
    """环境守卫：生产环境没有验证码日志，误调用直接炸，绝不静默返回错误数据。"""
    if not env.sms_log_url:
        raise RuntimeError(
            f"环境 [{env.name}] 未配置验证码日志。生产环境的验证码获取方式"
            f"需要单独方案（如后台查询接口），不能沿用测试环境的日志抓取。"
        )
    return env.sms_log_url

# 匹配一条短信记录：
#   时间戳(2026-09-28 18:56:08261) + receiver = 手机号 + message = 验证码NNNNNN
SMS_PATTERN = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\d*"   # 毫秒部分不进捕获组
    r".*?receiver\s*=\s*【?(?P<phone>\d{11})】?"
    r".*?message\s*=\s*【?验证码(?P<code>\d{4,8})】?"
)


def fetch_log_tail(max_bytes: int = 300_000) -> str:
    """只拉日志尾部，避免整文件下载。max_bytes 控制读取量。"""
    log_url = _require_log_url()
    # 先用 HEAD 拿到文件总大小
    head = requests.head(log_url, verify=env.verify_ssl, timeout=env.http_timeout)
    total = int(head.headers.get("Content-Length", 0))
    start = max(0, total - max_bytes)
    resp = requests.get(
        log_url,
        headers={"Range": f"bytes={start}-"},   # Range 请求：只要尾部
        verify=env.verify_ssl,
        timeout=env.http_timeout,
    )
    resp.raise_for_status()
    return resp.text


def parse_codes(log_text: str, phone: str) -> list[dict]:
    """从日志文本中筛出指定手机号的全部验证码记录，按时间升序返回。

    返回结构：[{"ts": datetime, "code": "613962"}, ...]
    """
    records = []
    for m in SMS_PATTERN.finditer(log_text):
        if m.group("phone") != phone:
            continue  # 只取目标号码，别人的短信全部忽略
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        records.append({"ts": ts, "code": m.group("code")})
    records.sort(key=lambda r: r["ts"])
    return records


def get_sms_code(phone: str, wait: bool = True, timeout: int = 60) -> str:
    """获取指定手机号当前有效的验证码。

    典型用法（在用例里）：
        trigger_login_sms(phone)        # 1. 先调发送验证码接口（文档到了补上）
        code = get_sms_code(phone)      # 2. 再从日志里等验证码出现

    wait=True 时轮询等待（发送到打进日志有几秒延迟）；
    拿不到有效验证码会抛 RuntimeError，让用例明确失败而不是拿旧码继续跑。
    """
    code_ttl = env.sms_code_ttl
    deadline = time.time() + timeout
    while True:
        records = parse_codes(fetch_log_tail(), phone)
        if records:
            latest = records[-1]
            age = datetime.now() - latest["ts"]
            # 时钟偏差容忍 30 秒；超过有效期的码不能用于登录
            if age.total_seconds() <= code_ttl + 30:
                return latest["code"]
        if not wait or time.time() > deadline:
            raise RuntimeError(
                f"未获取到 {phone} 在 {code_ttl // 60} 分钟内的有效验证码"
            )
        time.sleep(3)   # 每 3 秒重查一次日志


if __name__ == "__main__":
    # 自测：打印当前环境信息 + 日志里最新几条记录（号码脱敏）
    print(f"当前环境: {env.name}  (切换方式: MIGU_ENV=test|prod)")
    print(f"验证码日志: {env.sms_log_url or '（该环境未配置）'}")
    text = fetch_log_tail()
    seen = [
        f"{m.group('ts')}  {m.group('phone')[:3]}****{m.group('phone')[-4:]}  验证码={m.group('code')}"
        for m in SMS_PATTERN.finditer(text)
    ]
    print(f"日志尾部共解析出 {len(seen)} 条验证码记录，最近 5 条：")
    for line in seen[-5:]:
        print(" ", line)
