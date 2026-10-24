# -*- coding: utf-8 -*-
"""
c2login.py —— C 端「手机号 + 短信验证码」登录（生产环境的登录方式）

为什么需要它：
    测试环境的登录路径是    通行证会话 → ST token → hmcer
    （通行证 Cookie 靠浏览器登录一次，验证码从 app-7001.log 抓）

    但**生产没有验证码日志**，而且通行证是 passport.migu.cn（异域），
    这条路径在生产走不通。生产发的是**真实验证码**——只能真人看手机。

    好在好麦通自己就有一套手机号登录接口，不依赖通行证：
        POST /v1/sms/sendUserVerifyCode?storeId=888   {"mobileNo": "155..."}
             → 触发真实短信（实测 2026-09-29 返回 {"code":200}）
        POST /v2/login/mobileNo?storeId=888           {"mobileNo": "...", "verifyCode": "123456"}
             → 直接返回 {"code":200,"data":{"hmcer":"...", "tpid":..., "id":...}}

    所以生产登录 = **半自动**：脚本发码 → 人读短信填 6 位码 → 换 hmcer。
    一次登录之后 hmcer 长期有效（见下），不用每次都来。

hmcer 的复用与过期：
    登录成功后把 hmcer 写进 .migu_state/<env>/hmcer.json（与通行证路径同一个文件，
    下游 client/passport 完全无感），并额外记一份 c2_login.json 记录
    {phone, login_at, tpid, userId} —— 用于判断"这个登录态是不是要过期了"。

    实测 hmcer 是服务端会话，过期时间不确定；本模块不猜 TTL，而是：
      - 缓存存在 → 直接用
      - 接口真的返回 401/403 → client 会自动重试一次，重试仍失败
        → 说明该重新登录了，报错里会说清楚"需要新验证码"

用法：
    # 交互式（推荐）：发码 → 提示你输入手机收到的 6 位码
    MIGU_ENV=prod python -m migu_api.c2login

    # 非交互：验证码已从别处拿到
    MIGU_ENV=prod python -m migu_api.c2login --code 123456

    # 只看当前登录态缓存状态
    MIGU_ENV=prod python -m migu_api.c2login --status

    # 只发码（自己拿去别的脚本里用）
    MIGU_ENV=prod python -m migu_api.c2login --send-only
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

from . import passport
from .config import env
from .fsutil import ensure_dir

STATE_DIR = Path(__file__).resolve().parent.parent / ".migu_state" / env.state_dir_name
# 与通行证登录共用同一个 hmcer 文件 —— 换登录方式但下游无感
HMCER_FILE = STATE_DIR / "hmcer.json"
# 记录"这次 hmcer 是怎么来的、什么时候来的、属于谁"，用于过期判断与排障
META_FILE = STATE_DIR / "c2_login.json"


class C2LoginError(RuntimeError):
    """C 端登录失败（如验证码错误/过期、账号未登记）。"""


def default_phone() -> str:
    """取 config 里登记的第一个账号手机号，作为默认登录号码。"""
    phone = next(iter(env.accounts), None)
    if not phone:
        raise C2LoginError(
            f"环境 [{env.name}] 未登记测试账号（config.py 的 {env.name.upper()}.accounts），"
            "无法确定用哪个手机号登录"
        )
    return phone


# ---------------- 两个接口 ----------------

def send_code(phone: str | None = None) -> dict:
    """触发真实短信验证码。返回接口响应（成功时 code=200，message 通常为空）。"""
    phone = phone or default_phone()
    s = passport.build_session()
    resp = s.post(
        f"{env.api_base}/v1/sms/sendUserVerifyCode?storeId={env.store_id}",
        json={"mobileNo": phone},
        timeout=env.http_timeout,
    )
    try:
        body = resp.json()
    except ValueError:
        raise C2LoginError(f"发送验证码失败，HTTP {resp.status_code}: {resp.text[:300]}")
    if body.get("code") != 200:
        raise C2LoginError(f"发送验证码被拒绝: {body}")
    return body


def login(phone: str, code: str) -> dict:
    """用手机号 + 验证码换取 hmcer，成功后落盘。

    返回 data 字典（含 hmcer / tpid / id / userName / isNewUser）。
    """
    if not code or not str(code).strip():
        raise C2LoginError("验证码为空")
    code = str(code).strip()

    s = passport.build_session()
    resp = s.post(
        f"{env.api_base}/v2/login/mobileNo?storeId={env.store_id}",
        json={"mobileNo": phone, "verifyCode": code},
        timeout=env.http_timeout,
    )
    try:
        body = resp.json()
    except ValueError:
        raise C2LoginError(f"登录失败，HTTP {resp.status_code}: {resp.text[:300]}")

    data = body.get("data") or {}
    hmcer = data.get("hmcer")
    if body.get("code") != 200 or not hmcer:
        # 常见：验证码错误/过期（接口会带上提示），或账号在该店铺无权限
        hint = body.get("message") or body.get("errorCode") or ""
        raise C2LoginError(
            f"手机号登录被拒绝: code={body.get('code')} message={hint!r}。"
            "验证码 5 分钟内有效且只能用一次，请重新发码再试"
        )

    # 1) 写 hmcer —— 与通行证路径同一个文件，下游无感
    ensure_dir(HMCER_FILE.parent)
    HMCER_FILE.write_text(
        json.dumps({"hmcer": hmcer}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 2) 写元信息（来源/时间/身份），用于过期判断与排障
    META_FILE.write_text(
        json.dumps(
            {
                "source": "mobileNo+verifyCode",
                "phone": phone,
                "env": env.name,
                "storeId": env.store_id,
                "userId": data.get("id"),
                "tpid": data.get("tpid"),
                "userName": data.get("userName"),
                "isNewUser": data.get("isNewUser"),
                "loginAt": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return data


# ---------------- 从浏览器直接取 C 端登录态（免短信） ----------------

def _candidate_cookie_hosts() -> list[str]:
    """当前环境下可能携带 C 端 hmcer 的 Cookie 域（带前导点的父域靠后）。

    生产 H5 与通行证分属不同根域：接口在 ticket.miguvideo.com，
    但浏览器也可能把 hmcer 写在 .miguvideo.com 甚至 .migu.cn 上，
    所以这里列全，**能不能用交给会员接口去验证**（验证不过就丢弃）。
    """
    host = env.api_host
    hosts = [host, f".{host}"]
    parts = host.split(".")
    if len(parts) >= 2:
        hosts.append("." + ".".join(parts[-2:]))
    hosts.append(".migu.cn")
    return hosts


def _verify_hmcer(host: str, value: str) -> dict | None:
    """把候选 hmcer 挂到对应域上打一次会员接口：通过返回 data，不通过返回 None。"""
    s = passport.build_session()
    s.cookies.set("hmcer", value, domain=host.lstrip("."))
    try:
        resp = s.get(f"{env.api_base}/v1/customer/getInfoV2?storeId={env.store_id}",
                     timeout=env.http_timeout)
        body = resp.json()
    except (ValueError, requests.RequestException):
        return None
    return (body.get("data") or None) if body.get("code") == 200 else None


def harvest_from_browser(browser: str = "chrome", save: bool = True) -> dict:
    """从本机浏览器 Cookie 库取出**当前环境**的 C 端登录态 hmcer（免短信）。

    为什么需要它：
        生产登录走「手机号 + 真实验证码」，但短信有成本、还可能被网关拦
        （实测遇到过 `{"code":20102,"message":"发送短信失败"}`）。
        而只要你用浏览器打开过该环境的 H5 并登录过，Cookie 库里就有 hmcer —— 
        直接解出来即可，**不需要短信、不需要钥匙串以外的任何交互**。

    安全性：
        候选 Cookie 会挨个拿去打**当前环境**的会员接口，只有验证通过的那个才会落盘。
        所以既不会误用另一个环境的凭证，也不会写入已过期的登录态。

    返回 {"hmcer", "phone", "userId", "host"}；读不到可用登录态抛 C2LoginError。
    """
    # 延迟导入：bticket 会 import badmin → config，顶层互相 import 容易成环
    from .bticket import CHROMIUM_BROWSERS, _decrypt_v10, _keychain_password

    if browser not in CHROMIUM_BROWSERS:
        raise C2LoginError(f"暂不支持 {browser}，可选：{', '.join(CHROMIUM_BROWSERS)}")
    rel, service = CHROMIUM_BROWSERS[browser]
    db = Path.home() / "Library/Application Support" / rel / "Default" / "Cookies"
    if not db.exists():
        raise C2LoginError(f"找不到 {browser} 的 Cookie 库：{db}")

    pw = _keychain_password(service)
    if not pw:
        raise C2LoginError(
            f"读不到钥匙串里的 {service!r}。首次运行 macOS 会弹授权框，"
            "点『始终允许』后重跑即可。"
        )

    tmp = Path(tempfile.gettempdir()) / f"migu_c_{browser}_cookies.db"
    shutil.copy(db, tmp)          # 复制一份读，避免锁住浏览器正在用的库
    con = sqlite3.connect(tmp)
    rows = list(con.execute(
        "select host_key, name, encrypted_value from cookies "
        "where name in ('hmcer', 'hmer', 'tpid')"))
    con.close()

    allowed = set(_candidate_cookie_hosts())
    found: list[tuple[str, str]] = []
    for host, name, enc in rows:
        if name != "hmcer" or host not in allowed:
            continue
        val = _decrypt_v10(enc, pw)
        if val:
            found.append((host, val))
            print(f"  解出候选 hmcer：{host}（len={len(val)}）")

    for host, val in found:
        data = _verify_hmcer(host, val)
        if not data:
            print(f"  ✗ {host} 的 hmcer 在当前环境验证不通过（过期或属于别的环境），跳过")
            continue
        if save:
            ensure_dir(HMCER_FILE.parent)
            HMCER_FILE.write_text(
                json.dumps({"hmcer": val}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            META_FILE.write_text(
                json.dumps(
                    {
                        "source": f"harvest:{browser}({host})",
                        "phone": data.get("phone"),
                        "env": env.name,
                        "storeId": env.store_id,
                        "userName": data.get("nickname"),
                        "loginAt": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return {"hmcer": val, "phone": data.get("phone"),
                "userName": data.get("nickname"), "host": host}

    raise C2LoginError(
        f"在 {browser} 里没找到 {env.name} 环境可用的 C 端登录态。\n"
        f"  请在浏览器打开 {env.h5_url} 登录一次（或用 --send-only 发短信登录），再重跑。"
    )


# ---------------- 缓存读取 ----------------

def load_meta() -> dict | None:
    if not META_FILE.exists():
        return None
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def ensure_cached(phone: str | None = None) -> str:
    """返回一个缓存的 hmcer；没有缓存就抛错（并告诉你怎么登录）。

    ⚠️ 本函数**不会**自动发短信 —— 发短信需要真人读手机，
    不能藏在自动流程里静默触发（否则每次跑用例都给人发一条真短信）。
    """
    cached = passport._load_json(HMCER_FILE)
    if cached and cached.get("hmcer"):
        return cached["hmcer"]
    meta = load_meta()
    phone = phone or (meta or {}).get("phone") or next(iter(env.accounts), "<手机号>")
    raise C2LoginError(
        f"环境 [{env.name}] 没有可用的登录态（{HMCER_FILE} 不存在）。\n"
        "该环境的登录方式是「手机号 + 真实验证码」，需要真人参与一次：\n"
        f"    MIGU_ENV={env.name} python -m migu_api.c2login --phone {phone}\n"
        "脚本会发一条真实短信，你把手机收到的 6 位数字填进去即可，"
        "之后登录态会缓存复用，不用每次都来。"
    )


# ---------------- 命令行 ----------------

def _cli() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m migu_api.c2login",
        description="C 端手机号+短信验证码登录（生产环境用这条路径）",
    )
    ap.add_argument("--phone", default=None, help="手机号，默认取 config 里登记的第一个账号")
    ap.add_argument("--code", default=None,
                    help="验证码。不传则发码后交互式等待输入；也可用环境变量 MIGU_SMS_CODE")
    ap.add_argument("--send-only", action="store_true", help="只发验证码，不登录")
    ap.add_argument("--status", action="store_true", help="只打印当前登录态缓存状态")
    ap.add_argument("--harvest", action="store_true",
                    help="从本机浏览器取登录态（免短信；浏览器登录过该环境即可）")
    ap.add_argument("--browser", default="chrome",
                    choices=["chrome", "edge", "brave", "chromium"],
                    help="harvest 用，默认 chrome")
    ap.add_argument("--verify", action="store_true",
                    help="登录后用 /v1/customer/getInfoV2 复核身份（建议加）")
    args = ap.parse_args()

    print(f"环境 MIGU_ENV={env.name} | 接口 {env.api_base} | storeId={env.store_id}")

    if args.harvest:
        try:
            got = harvest_from_browser(browser=args.browser)
        except C2LoginError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"✓ 已从 {args.browser} 取到 {env.name} 环境的 C 端登录态"
              f"（{got.get('host')}，手机号 {got.get('phone')}）")
        print(f"  已写入 {HMCER_FILE}")
        return 0

    if args.status:
        meta = load_meta()
        h = passport._load_json(HMCER_FILE)
        injected = passport.injected_hmcer()
        print(f"hmcer 缓存: {'有' if h and h.get('hmcer') else '无'}  ({HMCER_FILE})")
        print(f"环境变量注入: "
              f"{'✅ 有（' + passport.HMCER_ENV + '，优先级最高，失效不会自动续期）' if injected else '无'}")
        if meta:
            print("登录来源  :", meta.get("source"))
            print("手机号    :", meta.get("phone"))
            print("登录时间  :", meta.get("loginAt"))
            print("用户      :", meta.get("userName"), "| userId:", meta.get("userId"),
                  "| tpid:", meta.get("tpid"))
        else:
            print("（没有 c2login 记录，可能是通行证路径登录的）")
        return 0

    phone = args.phone or default_phone()
    print(f"手机号    : {phone}")

    try:
        send_code(phone)
    except C2LoginError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ 验证码已发送到 {phone}（真机短信，5 分钟内有效）")

    if args.send_only:
        return 0

    code = args.code or os.environ.get("MIGU_SMS_CODE")
    if not code:
        try:
            code = input("请输入手机收到的 6 位验证码: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消", file=sys.stderr)
            return 130

    try:
        data = login(phone, code)
    except C2LoginError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    print(f"✓ 登录成功：userId={data.get('id')} tpid={data.get('tpid')} "
          f"userName={data.get('userName')}")
    print(f"  hmcer 已写入 {HMCER_FILE}（下游 client 会直接复用）")

    if args.verify:
        from .client import MiguClient
        c = MiguClient(phone=phone)
        c.login()
        info = c.get_member_info()
        got = info.get("phone")
        ok = got == phone
        print(f"{'✓' if ok else '✗'} 身份复核：登录态手机号={got}（期望 {phone}）")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
