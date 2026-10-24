# -*- coding: utf-8 -*-
"""
passport.py —— 登录凭证管理（本框架最核心的一块拼图）

实测确认的登录链路（2026-09-28 抓包逆向）：

    [一次性] 浏览器里走通行证登录（验证码从 app-7001.log 抓）
             → 通行证会话 Cookie（LTToken/USessionID/mgnd_*）存到本地文件

    [每次自动] GET /login?sourceid=203066&...
               → 通行证会话有效时 302，Location 里带 ST-xxx token（CAS 风格）
    [每次自动] POST /saishi/api/v3/login  body={"token": "ST-xxx", "storeId": "1"}
               → 返回 hmcer 凭证（业务登录态）
    [之后]     所有业务接口带 Cookie: hmcer=xxx 即为登录态

也就是说：浏览器登录一次，之后续期全自动，不再依赖浏览器。

凭证缓存文件（.migu_state/ 下）：
    passport_cookies.json  通行证会话（长期有效，失效需重新浏览器登录）
    hmcer.json             业务登录态（会过期，过期自动用上面的流程刷新）

CI / 容器里的登录态怎么来（重要）
    上面两条路径都要"先有人在浏览器里登录过一次"，CI 机器上没有浏览器，所以留了一条
    **注入通道**：把业务登录态直接放环境变量，优先级高于任何本地缓存文件。

        MIGU_C_HMCER='<hmcer 的值>'              # 只给值
        MIGU_C_HMCER='hmcer=xxx; tpid=1; ...'    # 或整段 Cookie 字符串，自动挑出 hmcer
        MIGU_C_HMCER='/abs/path/hmcer.json'      # 或指向一份 hmcer.json

    ⚠️ 这是**只读注入**：注入的凭证过期时不会（也无法）自动续期，会抛出明确错误，
       提示你去更新 CI Secret —— 这样流水线红了能一眼知道是"凭证过期"而不是"用例挂了"。
       取新值的办法：本地 ./run.sh login --harvest（从浏览器取）或人工走一次短信登录，
       然后把 .migu_state/<env>/hmcer.json 里的值更新到 Secret。
"""
import json
import os
import re
from pathlib import Path

import requests
import urllib3

from .config import env, PASSPORT_LOGIN_QUERY, BROWSER_UA
from .fsutil import ensure_dir

# requests 调自签证书会刷警告，测试环境直接关掉
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 凭证缓存目录：项目根/.migu_state/<环境名>/
# ⚠️ 必须按环境分子目录 —— 测试与生产的通行证域、hmcer 完全不同，
#    放在一起会在切环境时误用另一套 Cookie（详见 config.py 的 state_dir_name）
STATE_DIR = Path(__file__).resolve().parent.parent / ".migu_state" / env.state_dir_name
PASSPORT_COOKIE_FILE = STATE_DIR / "passport_cookies.json"
HMCER_FILE = STATE_DIR / "hmcer.json"

# CI 注入通道：环境变量里直接带业务登录态（见模块 docstring）
HMCER_ENV = "MIGU_C_HMCER"


def injected_hmcer() -> str | None:
    """从环境变量取注入的业务登录态；没注入返回 None。

    容错三种写法（CI 里手里有什么就能塞什么）：
        1) 直接是 hmcer 的值
        2) 整段 Cookie 字符串（含 hmcer=xxx）—— 自动挑出来
        3) 一个 hmcer.json 的路径 —— 读它的 hmcer 字段
    """
    raw = os.environ.get(HMCER_ENV)
    if raw is None:
        return None
    val = raw.strip()
    if not val:
        return None

    # 3) 路径写法
    if val.endswith(".json"):
        try:
            data = json.loads(Path(val).expanduser().read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("hmcer"):
                return str(data["hmcer"]).strip()
        except (OSError, json.JSONDecodeError):
            pass

    # 2) Cookie 整串写法：挑 hmcer=xxx（到 ; 或空白为止）
    m = re.search(r"(?:^|[;\s])hmcer=([^;\s]+)", val)
    if m:
        return m.group(1)

    # 1) 直接给值
    return val

# 通行证域名（不含协议），用于给 Cookie 指定 domain
PASSPORT_HOST = env.passport_base.split("//")[-1].strip("/")


def _save_json(path: Path, data) -> None:
    """写缓存文件前先建目录，写完打印提示方便排查。"""
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path):
    """文件不存在或损坏都返回 None，让上层走刷新流程而不是崩掉。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_session() -> requests.Session:
    """构造一个带公共请求头的 Session。

    WAF 规则（实测）：UA 不是浏览器的直接 403，Referer/Origin 缺了也拦。
    所以这三个头对所有请求统一带上，一劳永逸。
    """
    s = requests.Session()
    s.verify = env.verify_ssl
    s.headers.update({
        "User-Agent": BROWSER_UA,
        "X-ClientType": "h5",
        "X-StoreId": env.store_id,
        "Referer": env.h5_url,
        "Origin": env.base_url,
        "Accept": "application/json, text/plain, */*",
    })
    if not env.verify_ssl:
        # 测试环境自签证书，明确关掉校验（生产 profile 的 verify_ssl=True 不会走到这）
        import urllib3 as _u3
        _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)
    return s


def save_passport_cookies(cookies: dict) -> None:
    """把浏览器导出的通行证 Cookie 存入缓存（浏览器登录后人工/脚本调用一次）。"""
    _save_json(PASSPORT_COOKIE_FILE, cookies)


def _passport_session_alive(session: requests.Session) -> bool:
    """判断通行证会话是否有效：访问 /login，302 = 有效，200 登录页 = 已失效。"""
    url = f"{env.passport_base}/login?{PASSPORT_LOGIN_QUERY}"
    resp = session.get(url, allow_redirects=False, timeout=env.http_timeout)
    return resp.status_code in (301, 302, 303, 307, 308)


def get_st_token(session: requests.Session) -> str:
    """用通行证会话换取一次性 ST token。

    通行证会话有效时 GET /login 会 302 到 auth/index?token=ST-xxx&...
    这个 token 是 CAS 风格的一次性票据，换一次就作废。
    """
    url = f"{env.passport_base}/login?{PASSPORT_LOGIN_QUERY}"
    resp = session.get(url, allow_redirects=False, timeout=env.http_timeout)

    if resp.status_code not in (301, 302, 303, 307, 308):
        raise RuntimeError(
            "通行证会话已失效（/login 未跳转而是返回登录页）。"
            f"请重新在浏览器登录一次并把 Cookie 存入 {PASSPORT_COOKIE_FILE}"
        )

    location = resp.headers.get("Location", "")
    m = re.search(r"[?&]token=([^&]+)", location)
    if not m:
        raise RuntimeError(f"302 跳转里没有 token 参数，Location={location!r}")
    return m.group(1)


def v3_login(session: requests.Session, token: str) -> str:
    """用 ST token 调 /v3/login 换取业务登录态 hmcer。

    响应体：{"code":200, "data":{"id":..., "hmcer":"...", "tpid":..., ...}}
    hmcer 就是之后所有业务接口的鉴权凭证（以 Cookie 形式携带）。
    """
    resp = session.post(
        f"{env.api_base}/v3/login",
        json={"token": token, "storeId": env.store_id},
        timeout=env.http_timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200 or not body.get("data", {}).get("hmcer"):
        raise RuntimeError(f"/v3/login 失败: {body}")
    return body["data"]["hmcer"]


def refresh_hmcer() -> str:
    """完整刷新流程：通行证 Cookie → ST token → hmcer，结果写缓存。"""
    s = build_session()
    cookies = _load_json(PASSPORT_COOKIE_FILE)
    if not cookies:
        raise RuntimeError(
            f"找不到通行证 Cookie 缓存（{PASSPORT_COOKIE_FILE}）。"
            "首次使用需要先在浏览器完成一次登录：见项目 README 的引导步骤"
        )
    # 通行证 Cookie 逐个塞进 session（dict -> cookiejar）
    # 注意域要用当前环境的通行证域：测试 union-passport.migu.cn / 生产 passport.migu.cn
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=PASSPORT_HOST)

    token = get_st_token(s)
    hmcer = v3_login(s, token)
    _save_json(HMCER_FILE, {"hmcer": hmcer})
    return hmcer


def ensure_hmcer() -> str:
    """拿一个可用的 hmcer：有缓存先用缓存，接口打脸再刷新。

    返回的 hmcer 不保证一定有效（可能刚好过期），调用方遇到 401
    应该调 force_refresh_hmcer() 再重试一次。

    没有缓存时的取舍：
        - 环境变量注入（MIGU_C_HMCER，CI 用）→ 直接用，优先级最高
        - 有通行证 Cookie（测试环境的常规状态）→ 走通行证刷新
        - 没有通行证 Cookie（生产环境没有验证码日志，压根没这条路径）
          → 尝试手机号登录留下的缓存，并在报错里说清"需要真人给验证码"
    """
    injected = injected_hmcer()
    if injected:
        return injected
    cached = _load_json(HMCER_FILE)
    if cached and cached.get("hmcer"):
        return cached["hmcer"]
    if PASSPORT_COOKIE_FILE.exists():
        return refresh_hmcer()
    # 延迟导入：c2login 反过来会用 passport.build_session，顶层互相 import 会成环
    from . import c2login
    return c2login.ensure_cached()


def force_refresh_hmcer() -> str:
    """强制刷新业务登录态（hmcer 过期时的恢复路径）。

    通行证 Cookie 还在 → 自动续期；否则（生产）只能靠重新做一次手机号登录。
    走注入通道（MIGU_C_HMCER）时**无法自动续期** —— 直接抛错讲清楚怎么换新值，
    免得流水线报一个看不懂的 401。
    """
    if injected_hmcer():
        raise RuntimeError(
            f"业务登录态是通过环境变量 {HMCER_ENV} 注入的，已失效且**无法自动续期**。\n"
            "请更新 CI Secret（或本地环境变量）里的 hmcer：\n"
            "  1) 本地取一份新的：./run.sh login --harvest（从浏览器取，免短信）\n"
            "     或 MIGU_ENV=<env> ./run.sh login --code <验证码>（人工短信登录一次）\n"
            f"  2) 把 .migu_state/{env.state_dir_name}/hmcer.json 里的 hmcer 值更新到 Secret\n"
            f"  3) 若只是想临时绕过，可 unset {HMCER_ENV} 让它改走本地缓存文件"
        )
    if PASSPORT_COOKIE_FILE.exists():
        try:
            return refresh_hmcer()
        except RuntimeError:
            pass  # 通行证会话失效了，往下走手机号登录的提示
    from . import c2login
    return c2login.ensure_cached()


# ---------------- 命令行自测 ----------------
if __name__ == "__main__":
    print("== 1) 检查通行证 Cookie 缓存")
    print("   ", PASSPORT_COOKIE_FILE, "存在" if PASSPORT_COOKIE_FILE.exists() else "不存在")
    print("== 2) 尝试换取 hmcer")
    try:
        h = refresh_hmcer()
        print(f"    成功: {h[:20]}...")
    except RuntimeError as e:
        print(f"    失败: {e}")
