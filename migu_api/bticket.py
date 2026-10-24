# -*- coding: utf-8 -*-
"""
bticket.py —— B 端登录态（4A 票据 → 会话）的获取与自动维持

============================================================================
先把事实说准：票据**不是一次性的**，但会过期（寿命以天计）
============================================================================
B 端登录走 4A 单点登录，入口 URL 上带票据：

    https://ticket.miguvideo.com/bfront/?iamcaspticket=<239 字符，内含换行>

实测（2026-09-29，把浏览器历史里 100+ 条票据按所属环境全量重放）：

    1) **同一张票据可以重复兑换**：连兑 3 次全部 code=200；
       同一张票换出的两个会话并存、互不踢下线。→ 不是一次性。
       （更正：早前一条"票据是一次性的"结论是**方法错了** ——
         当时用默认的**测试环境** API 去兑历史里的**生产**票据，
         跨环境兑换必然失败，被误读成了"用过即废"。）

    2) **票据寿命以"天"计**：生产票据放到 7.8 天后、测试票据放到 11.7 天后
       兑换依然成功。所以"你点一次 4A 入口，脚本事后去捞票"这条路**完全可行**，
       不需要抢在浏览器消费之前取票。

    3) 失败样本都能归因，**不是过期**：
       · URL 属于 4A 门户自身（10.181.58.56:8444/amsportal/login.do?iamcaspticket=…）
         —— 那是门户上下文的票据，跟 B 端应用不是一回事；
       · 别的主机（migu.haomaitong.com:18084、10.181.58.10:8444）；
       · 早期"本系统无此账号"（历史账号变更）。
       所以捞票时**必须按 URL 主机名筛出当前环境的应用票据**（见 _ticket_host_ok）。

--------------------------------------------------------------------------
恢复链路（从最便宜到最贵，全自动优先）
--------------------------------------------------------------------------
0. `status`      会话还在不在（只读 /v1/user，不产生任何业务数据）
1. `keepalive`   ★零人工：定时打心跳，会话不死就永远不需要票据
2. `renew`       ★零人工、零授权：会话真死了 → 从浏览器 History 里按时间倒序
                 捞出**当前环境**的应用票据，逐个兑换，成功即落盘。
                 读 History **不需要钥匙串授权**（History 库是明文 sqlite），
                 所以这条比 harvest 还省事。
3. `harvest`     从浏览器 Cookie 库直接取会话（需要一次钥匙串授权）
4. `paste`       兜底：手工粘 4A 入口 URL 或 hmer 值
5. 4A CAS 直连   全自动但需企业账号密码，见 `cas` 子命令的调研结论（暂不推荐）

`ensure` 是给 run.sh 用的：会话有效 → 0；失效 → 依次尝试 renew / harvest，
都失败才打印人工指引。也就是说**核销链路平时不需要人工介入**。

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    python -m migu_api.bticket status
    python -m migu_api.bticket keepalive
    python -m migu_api.bticket renew           # 从浏览器历史捞票换会话（无需授权）
    python -m migu_api.bticket renew --list    # 只看捞到哪些候选票，不兑换
    python -m migu_api.bticket harvest         # 从 Chrome Cookie 库取会话
    python -m migu_api.bticket paste '<URL 或 hmer 值>' [--hmer]
    python -m migu_api.bticket ensure          # run.sh 用：自动恢复 + 指引
    python -m migu_api.bticket cas | schedule

环境：默认测试环境，生产加 MIGU_B_ENV=prod。
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .badmin import BAdminClient, BAdminAuthError, B_COOKIE_FILE
from .config import b_env
from .fsutil import ensure_dir

STATE_DIR = Path(__file__).resolve().parent.parent / ".migu_state" / b_env.state_dir_name
# 保活/会话体检记录：{lastOk, lastCheck, userName, userId, cookieSavedAt, okCount, failCount}
HEALTH_FILE = STATE_DIR / "b_session.json"


# ============================================================================
# 会话体检与保活
# ============================================================================

def _load_health() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_health(data: dict) -> None:
    ensure_dir(HEALTH_FILE.parent)
    HEALTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def probe_session(client: BAdminClient | None = None) -> dict:
    """探一次会话，返回 {ok, userName, userId, error, http}。

    只调 /v1/user（只读、轻量），不会产生任何业务数据。
    """
    c = client or BAdminClient()
    try:
        me = c._request("GET", "/v1/user")
        return {"ok": True, "userName": me.get("userName"),
                "userId": me.get("id"), "roles": me.get("roles")}
    except BAdminAuthError as e:
        return {"ok": False, "error": f"登录态失效（HTTP {e.status}）"}
    except Exception as e:  # 网络/网关问题不等于会话失效，区分开
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def cmd_status(as_json: bool = False) -> int:
    """打印会话状态 + 历史保活记录。"""
    h = _load_health()
    cookie_at = None
    if B_COOKIE_FILE.exists():
        cookie_at = datetime.fromtimestamp(B_COOKIE_FILE.stat().st_mtime)
    r = probe_session()
    info = {
        "env": b_env.name,
        "api": b_env.api_base,
        "storeId": b_env.store_id,
        "cookieFile": str(B_COOKIE_FILE),
        "cookieSavedAt": cookie_at.isoformat(timespec="seconds") if cookie_at else None,
        "alive": r["ok"],
        "userName": r.get("userName"),
        "userId": r.get("userId"),
        "error": r.get("error"),
        "lastOk": h.get("lastOk"),
        "lastCheck": h.get("lastCheck"),
        "okCount": h.get("okCount"),
        "failCount": h.get("failCount"),
    }
    if as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    print(f"环境        : {b_env.name}  ({b_env.api_base}, storeId={b_env.store_id})")
    print(f"会话文件    : {B_COOKIE_FILE}")
    print(f"落盘时间    : {cookie_at:%Y-%m-%d %H:%M:%S}" if cookie_at else "落盘时间    : （无）")
    if r["ok"]:
        print(f"会话状态    : ✅ 有效（登录用户 {r.get('userName')}，userId={r.get('userId')}）")
    else:
        print(f"会话状态    : ❌ {r.get('error')}")
    if h.get("lastOk"):
        print(f"上次保活成功: {h['lastOk']}   累计成功 {h.get('okCount', 0)} 次 / "
              f"失败 {h.get('failCount', 0)} 次")
    if not r["ok"]:
        print("\n恢复会话的方式（前两条都不用人工）：")
        print("  python -m migu_api.bticket renew         # 从浏览器历史捞票换会话（推荐）")
        print("  python -m migu_api.bticket harvest       # 从浏览器 Cookie 库取（需钥匙串授权）")
        print("  python -m migu_api.bticket paste '<4A 入口 URL 或 hmer 值>'")
        if b_env.four_a_portal:
            print(f"  · 兜底：浏览器打开 4A 门户点一次应用 {b_env.four_a_portal}")
    return 0 if r["ok"] else 1


def cmd_keepalive(quiet: bool = False) -> int:
    """心跳：让会话保持活跃，避免被服务端回收。"""
    h = _load_health()
    now = datetime.now().isoformat(timespec="seconds")
    r = probe_session()
    h["lastCheck"] = now
    if r["ok"]:
        h["lastOk"] = now
        h["okCount"] = h.get("okCount", 0) + 1
        h["userName"] = r.get("userName")
        h["userId"] = r.get("userId")
        h["failCount"] = 0
        _save_health(h)
        if not quiet:
            print(f"✓ [{now}] 会话保活成功（{r.get('userName')}）")
        return 0
    h["failCount"] = h.get("failCount", 0) + 1
    _save_health(h)
    if not quiet:
        print(f"✗ [{now}] 会话保活失败：{r.get('error')}", file=sys.stderr)
        if h["failCount"] >= 2 and h.get("lastOk"):
            print(f"  注意：已连续失败 {h['failCount']} 次（上次成功 {h['lastOk']}），"
                  "会话很可能真的失效了，需要重新获取（见 bticket status）", file=sys.stderr)
    return 1


def cmd_ensure(quiet: bool = False, browser: str = "chrome",
               auto_renew: bool = True) -> int:
    """给 run.sh 用：会话可用 → 0；不可用 → **自动恢复**，仍失败才给人工指引。

    恢复顺序（前两步都不需要人工）：
        1) 已缓存的会话还在不在（只读自检）
        2) MIGU_B_TICKET 环境变量（老用法：手工塞一张票）
        3) renew   —— 从浏览器历史捞票换会话（读 History，无需钥匙串授权）
        4) harvest —— 从浏览器 Cookie 库直接取会话（需一次钥匙串授权）

    设 MIGU_B_NO_AUTO_RENEW=1 可关掉自动恢复（只想看状态、不想动浏览器数据时用）。
    """
    if os.environ.get("MIGU_B_NO_AUTO_RENEW") == "1":
        auto_renew = False

    r = probe_session()
    if r["ok"]:
        if not quiet:
            print(f"✓ B 端会话有效（{r.get('userName')}）")
        return 0

    if not quiet:
        print(f"⚠️  B 端会话不可用（{r.get('error')}）"
              + ("，尝试自动恢复…" if auto_renew else ""), file=sys.stderr)
    if not auto_renew:
        _print_manual_recovery()
        return 1

    if os.environ.get("MIGU_B_TICKET"):
        try:
            BAdminClient(ticket=os.environ["MIGU_B_TICKET"]).login()
            r = probe_session()
            if r["ok"]:
                if not quiet:
                    print(f"✓ 已用 MIGU_B_TICKET 恢复会话（{r.get('userName')}）")
                return 0
        except Exception as e:
            if not quiet:
                print(f"  · MIGU_B_TICKET 换会话失败：{e}", file=sys.stderr)

    # 自动恢复 ①：从浏览器历史捞票（最快、免授权）
    try:
        if cmd_renew(browser=browser, quiet=True) == 0:
            r = probe_session()
            if not quiet:
                print(f"✓ 已从浏览器历史捞票自动恢复会话（{r.get('userName')}）")
            return 0
    except Exception as e:
        if not quiet:
            print(f"  · renew 失败：{type(e).__name__}: {e}", file=sys.stderr)

    # 自动恢复 ②：从浏览器 Cookie 库取会话（需钥匙串授权）
    try:
        if cmd_harvest(browser=browser, quiet=True) == 0:
            r = probe_session()
            if not quiet:
                print(f"✓ 已从浏览器 Cookie 自动恢复会话（{r.get('userName')}）")
            return 0
    except Exception as e:
        if not quiet:
            print(f"  · harvest 失败：{type(e).__name__}: {e}", file=sys.stderr)

    if not quiet:
        print("✗ 自动恢复失败，核销类用例会被跳过。", file=sys.stderr)
        _print_manual_recovery()
    return 1


def _print_manual_recovery() -> None:
    """打印人工恢复指引（自动恢复失败后的兜底）。"""
    print("   手工恢复方式（任选其一）：", file=sys.stderr)
    if b_env.four_a_portal:
        print(f"     1) 浏览器打开 4A 门户点一次应用：{b_env.four_a_portal}",
              file=sys.stderr)
    print("     2) python -m migu_api.bticket renew    # 点过之后再跑一次，会自动捞票",
          file=sys.stderr)
    print("     3) python -m migu_api.bticket paste '<4A 入口 URL>'", file=sys.stderr)


def cmd_schedule(hours: int = 1) -> int:
    """打印"定时保活"的配置片段（cron / launchd），由用户自己决定装不装。"""
    py = str(Path(sys.executable))
    proj = Path(__file__).resolve().parent.parent
    print("=" * 74)
    print("让会话长期有效的推荐做法：每小时打一次心跳（keepalive）")
    print("=" * 74)
    print("\n【方案 A · crontab（最简单）】\n")
    print(f"  # 编辑：crontab -e")
    print(f"  0 * * * * cd {proj} && MIGU_B_ENV=prod \\")
    print(f"      {py} -m migu_api.bticket keepalive --quiet "
          f">> .migu_state/prod/keepalive.log 2>&1")
    print("\n【方案 B · launchd（macOS 原生，休眠唤醒后也会补跑）】\n")
    plist = f"""  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>cn.migu.bticket.keepalive</string>
    <key>ProgramArguments</key><array>
      <string>{py}</string><string>-m</string><string>migu_api.bticket</string>
      <string>keepalive</string><string>--quiet</string>
    </array>
    <key>WorkingDirectory</key><string>{proj}</string>
    <key>EnvironmentVariables</key><dict>
      <key>MIGU_B_ENV</key><string>prod</string>
    </dict>
    <key>StartInterval</key><integer>{hours * 3600}</integer>
    <key>RunAtLoad</key><true/>
  </dict></plist>"""
    print(plist)
    print(f"  存成 ~/Library/LaunchAgents/cn.migu.bticket.keepalive.plist 后：")
    print("    launchctl load -w ~/Library/LaunchAgents/cn.migu.bticket.keepalive.plist")
    print("\n说明：心跳只调只读接口 /v1/user，不产生任何业务数据。")
    print("     如果哪天心跳开始连续失败，说明会话真的被回收了 —— 此时不需要人工：")
    print("         python -m migu_api.bticket renew    # 从浏览器历史捞票，自动换回会话")
    print("     （票据不是一次性的、寿命以天计，只要最近进过一次 B 端后台就能捞到。")
    print("       读历史库不需要钥匙串授权。如需彻底无人化，见 `bticket cas`。）")
    return 0


# ============================================================================
# 票据 / 会话的录入
# ============================================================================

_TICKET_RE = re.compile(r"iamcaspticket=([^&\s]+)")


def extract_ticket(text: str) -> str | None:
    """从"一整条 URL"或"裸票据"里取出票据值。

    坑：票据本身含换行，在 URL 里是 %0A / %2F 这种百分号转义形式。
    这里统一**解一层百分号转义**，交给 requests 重新编码（与前端行为一致）。
    如果用户粘的已经是解码后的明文，就不再动它。
    """
    text = (text or "").strip()
    if not text:
        return None
    m = _TICKET_RE.search(text)
    raw = m.group(1) if m else text
    raw = raw.strip()
    if re.search(r"%[0-9A-Fa-f]{2}", raw):   # 还带着转义 → 解一层
        raw = unquote(raw)
    return raw


def cmd_paste(text: str, is_hmer: bool = False) -> int:
    """手工录入：--hmer 时直接写会话 Cookie；否则当作 4A 票据去换会话。"""
    if is_hmer:
        hmer = text.strip()
        if not hmer:
            print("✗ hmer 为空", file=sys.stderr)
            return 1
        # 只把 hmer 写进 Cookie 文件；其余 Cookie（hmid/ct/ll/dvid）保留原值
        cookies = []
        if B_COOKIE_FILE.exists():
            try:
                cookies = json.loads(B_COOKIE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cookies = []
        cookies = [c for c in cookies if c.get("name") != "hmer"]
        cookies.append({"name": "hmer", "value": hmer,
                        "domain": ".miguvideo.com", "path": "/"})
        ensure_dir(B_COOKIE_FILE.parent)
        B_COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print("✓ 已写入 hmer，正在校验…")
    else:
        ticket = extract_ticket(text)
        if not ticket:
            print("✗ 没解析出票据。请粘贴 4A 入口 URL 或票据原文", file=sys.stderr)
            return 1
        print(f"票据长度 {len(ticket)}（前缀 {ticket[:12]!r}），正在换取会话…")
        try:
            BAdminClient(ticket=ticket).login()
        except Exception as e:
            print(f"✗ 换会话失败：{e}", file=sys.stderr)
            print("  常见原因：票据属于**另一个环境**（测试票只能兑测试后台），"
                  "或这张票在 4A 侧已过期。\n"
                  "  可以先跑 `renew` 自动挑一张当前环境还能用的票。", file=sys.stderr)
            return 1
        print("✓ 换会话成功，已落盘")

    r = probe_session()
    if r["ok"]:
        h = _load_health()
        h.update({"lastOk": datetime.now().isoformat(timespec="seconds"),
                  "userName": r.get("userName"), "userId": r.get("userId"),
                  "failCount": 0})
        _save_health(h)
        print(f"✓ 会话可用（{r.get('userName')}）")
        return 0
    print(f"✗ 会话仍不可用：{r.get('error')}", file=sys.stderr)
    return 1


# ============================================================================
# 从本机浏览器里取 B 端会话（Chrome / Edge / Brave / Chromium）
# ============================================================================

CHROMIUM_BROWSERS = {
    "chrome": ("Google/Chrome", "Chrome Safe Storage"),
    "edge": ("Microsoft Edge", "Microsoft Edge Safe Storage"),
    "brave": ("BraveSoftware/Brave-Browser", "Brave Safe Storage"),
    "chromium": ("Chromium", "Chromium Safe Storage"),
}

# B 端会话需要的 Cookie 名（.miguvideo.com 域）
B_SESSION_COOKIES = ("hmer", "hmid", "hmfid", "ct", "ll", "dvid")


def _keychain_password(service: str) -> str | None:
    """读钥匙串里的浏览器主密钥。首次会弹授权框，点『始终允许』。"""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _decrypt_v10(blob: bytes, password: str) -> str | None:
    """解密 Chromium 的 v10 Cookie（AES-128-CBC，密钥由 PBKDF2-SHA1 派生）。

    优先用 pycryptodome；没装就退回 openssl 命令行（macOS 自带），
    这样本模块**不强制**任何新依赖。
    """
    if not blob.startswith(b"v10"):
        return None
    data = None
    try:
        from Crypto.Cipher import AES
        # 注意 pycryptodome 的参数名是 dkLen（不是 hashlib 风格的 dklen）
        key = _pbkdf2_sha1(password, b"saltysalt", 1003, 16)
        data = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(blob[3:])
    except ImportError:
        data = _aes_cbc_decrypt_openssl(blob[3:], password)
    if data is None:
        return None

    # 去掉 PKCS7 填充
    if data and 1 <= data[-1] <= 16:
        data = data[: -data[-1]]
    # 新版 Chrome 会在明文前拼 32 字节的 host 摘要，两种都试
    for cand in (data, data[32:] if len(data) > 32 else data):
        try:
            s = cand.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if s and s.isprintable():
            return s
    return None


def _aes_cbc_decrypt_openssl(blob: bytes, password: str) -> bytes | None:
    """没有 pycryptodome 时，用系统 openssl 做 AES-128-CBC 解密。"""
    key = _pbkdf2_sha1(password, b"saltysalt", 1003, 16)
    try:
        p = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-nopad",
             "-K", key.hex(), "-iv", (b" " * 16).hex()],
            input=blob, capture_output=True, timeout=20,
        )
        return p.stdout if p.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _pbkdf2_sha1(password: str, salt: bytes, iterations: int, dklen: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha1", password.encode(), salt, iterations, dklen)


def harvest_cookies(browser: str = "chrome") -> list[dict]:
    """从浏览器 Cookie 库里读出 .miguvideo.com 域的 B 端会话 Cookie。

    返回 [{"name","value","domain","path"}, ...]；读不到返回空列表。
    """
    if browser not in CHROMIUM_BROWSERS:
        raise ValueError(f"暂不支持 {browser}，可选：{', '.join(CHROMIUM_BROWSERS)}")
    rel, service = CHROMIUM_BROWSERS[browser]
    root = Path.home() / "Library/Application Support" / rel
    db = root / "Default" / "Cookies"
    if not db.exists():
        raise FileNotFoundError(f"找不到 {browser} 的 Cookie 库：{db}")

    pw = _keychain_password(service)
    if not pw:
        raise PermissionError(
            f"读不到钥匙串里的 {service!r}。\n"
            "  · 首次运行 macOS 会弹一个授权框，点『始终允许』即可；\n"
            "  · 如果是超时，多半是授权框没被点到（在后台），重跑一次并留意弹窗。"
        )

    tmp = Path(tempfile.gettempdir()) / f"migu_{browser}_cookies.db"
    shutil.copy(db, tmp)             # 复制一份读，避免锁住浏览器正在用的库
    con = sqlite3.connect(tmp)
    rows = list(con.execute(
        "select host_key, name, encrypted_value, path from cookies "
        "where host_key like '%miguvideo.com'"))
    con.close()

    wanted = set(B_SESSION_COOKIES) | {"hmcer", "tpid"}
    out, seen = [], set()
    for host, name, enc, path in rows:
        if name not in wanted or name in seen:
            continue
        val = _decrypt_v10(enc, pw)
        if not val:
            continue
        out.append({"name": name, "value": val, "domain": host, "path": path or "/"})
        seen.add(name)
    return out


def cmd_harvest(browser: str = "chrome", save: bool = True, quiet: bool = False) -> int:
    """从浏览器取 B 端会话，**先校验再落盘**。

    ⚠️ 重要：测试环境和生产环境的 B 端会话 Cookie 都挂在 `.miguvideo.com` 域上！
    浏览器里只会保留"最近一次登录"的那一个，所以 harvest 拿到的可能是**另一个环境**的会话。
    因此这里绝不直接覆盖当前环境的会话文件 —— 先写到临时文件试跑，
    确认能在**当前环境**通过校验，才真正落盘。
    """
    def say(*a):
        if not quiet:
            print(*a)

    say(f"从 {browser} 的 Cookie 库里读 B 端会话…")
    say("（会读取钥匙串条目做解密 —— 首次运行请在弹窗里点『始终允许』）")
    try:
        cookies = harvest_cookies(browser)
    except (PermissionError, FileNotFoundError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        print("\n替代方案：python -m migu_api.bticket renew"
              "（从浏览器历史捞票，不需要钥匙串授权）\n"
              "    或 python -m migu_api.bticket paste '<hmer 值>' --hmer", file=sys.stderr)
        return 1
    if not cookies:
        print("✗ 没读到 B 端会话 Cookie。请先在浏览器里从 4A 入口进入 B 端后台，再重试。",
              file=sys.stderr)
        return 1

    say("读到:", ", ".join(c["name"] for c in cookies))
    got_fid = next((c["value"] for c in cookies if c["name"] == "hmfid"), "?")
    say(f"其中 hmfid={got_fid}"
        "（测试环境=1 / 生产环境=2，两个环境的域都是 .miguvideo.com，会互相覆盖）")

    # 先落到临时文件，在当前环境上试跑
    ensure_dir(STATE_DIR)
    trial = STATE_DIR / "b_cookies.harvested.json"
    trial.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    r = probe_session(BAdminClient(cookie_file=trial))
    if not r["ok"]:
        trial.unlink(missing_ok=True)
        print(f"✗ 这份会话在当前环境 [{b_env.name}] 校验不通过：{r.get('error')}", file=sys.stderr)
        print(f"  说明浏览器里那次登录的不是 {b_env.name} 环境的后台。", file=sys.stderr)
        if b_env.four_a_portal:
            print(f"  请先在浏览器里从 4A 门户进入 {b_env.name} 的后台，再重跑 harvest。",
                  file=sys.stderr)
        print("  现有会话文件未被改动。", file=sys.stderr)
        return 1

    if save:
        shutil.copy(trial, B_COOKIE_FILE)
        trial.unlink(missing_ok=True)
        say(f"✓ 已写入 {B_COOKIE_FILE}")
    else:
        trial.unlink(missing_ok=True)
        say("（--no-save，未落盘）")

    h = _load_health()
    h.update({"lastOk": datetime.now().isoformat(timespec="seconds"),
              "userName": r.get("userName"), "userId": r.get("userId"),
              "source": f"harvest:{browser}", "failCount": 0})
    _save_health(h)
    say(f"✓ 会话可用（{r.get('userName')}，userId={r.get('userId')}）")
    say("  建议挂上保活：python -m migu_api.bticket keepalive（每小时一次）")
    return 0


# ============================================================================
# 从浏览器 History 里捞 4A 票据换会话（renew）
# ----------------------------------------------------------------------------
# 这是**比 harvest 更省事**的恢复方式：History 库是明文 sqlite，读它不需要钥匙串授权。
# 之所以可行，是因为票据可重复兑换、且寿命以天计（见模块头部的实测结论）。
# ============================================================================

def _ticket_host_ok(host: str) -> bool:
    """票据 URL 的主机名是否属于**当前环境**的 B 端应用。

    必须筛：4A 门户自己的 URL 上也挂着 iamcaspticket（10.181.58.56:8444/amsportal/…），
    那是门户上下文的票，拿去换 B 端会话会得到 "4A 认证失败:失败"；
    历史上还出现过 migu.haomaitong.com:18084、10.181.58.10:8444 等别的主机。
    """
    h = (host or "").split(":")[0].lower()
    if b_env.name == "test":
        return h == "ticket-test.miguvideo.com"
    return h == "ticket.miguvideo.com"


def history_dbs(browser: str = "chrome") -> list[Path]:
    """列出该浏览器所有 profile 的 History 库（Default / Profile 1 / …）。"""
    if browser not in CHROMIUM_BROWSERS:
        raise ValueError(f"暂不支持 {browser}，可选：{', '.join(CHROMIUM_BROWSERS)}")
    rel, _ = CHROMIUM_BROWSERS[browser]
    root = Path.home() / "Library/Application Support" / rel
    if not root.exists():
        raise FileNotFoundError(f"找不到 {browser} 的目录：{root}")
    return sorted(p for p in root.glob("*/History") if p.is_file())


def harvest_tickets(browser: str = "chrome", limit: int = 30) -> list[dict]:
    """捞出当前环境的历史 4A 票据，按访问时间倒序（同票去重）。

    返回 [{"ticket","url","host","visitedAt"}...]。**不需要钥匙串授权。**
    """
    EPOCH_1601 = 11644473600000000          # Chromium 时间戳基准（1601-01-01，微秒）
    out: list[dict] = []
    for db in history_dbs(browser):
        tmp = Path(tempfile.gettempdir()) / f"migu_{db.parent.name}_{db.name}"
        try:
            shutil.copy(db, tmp)            # 浏览器可能锁着原库，复制一份读
        except OSError:
            continue
        try:
            con = sqlite3.connect(tmp)
            rows = con.execute(
                "select url, last_visit_time from urls "
                "where url like '%iamcaspticket%' order by last_visit_time desc limit ?",
                (limit * 3,),
            ).fetchall()
            con.close()
        except sqlite3.DatabaseError:
            continue
        finally:
            tmp.unlink(missing_ok=True)

        for url, ts in rows:
            u = urlparse(url)
            if not _ticket_host_ok(u.netloc):
                continue
            ticket = parse_qs(u.query).get("iamcaspticket", [""])[0]
            if not ticket:
                continue
            out.append({
                "ticket": ticket, "url": url, "host": u.netloc,
                "visitedAt": datetime.fromtimestamp((ts - EPOCH_1601) / 1_000_000),
            })

    out.sort(key=lambda x: x["visitedAt"], reverse=True)
    uniq, seen = [], set()                  # Chrome 会同时记 /bfront 和 /bfront/ 两条
    for t in out:
        if t["ticket"] in seen:
            continue
        seen.add(t["ticket"])
        uniq.append(t)
    return uniq[:limit]


def _fmt_age(delta) -> str:
    if delta.days >= 1:
        return f"{delta.total_seconds()/86400:.1f} 天"
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f} 小时" if hours >= 1 else f"{delta.total_seconds()/60:.0f} 分钟"


def cmd_renew(browser: str = "chrome", max_try: int = 10, list_only: bool = False,
              quiet: bool = False) -> int:
    """会话失效时的**全自动**恢复：浏览器历史捞票 → 逐个兑换 → 成功即落盘。

    为什么这条路成立（2026-09-29 实测）：票据不是一次性的、寿命以天计、
    且读 History 不需要钥匙串授权。也就是只要你最近在浏览器里进过一次 B 端后台，
    这条命令就能在**无人工、无授权**的情况下把会话拿回来。

    兑换时先写到临时文件，校验通过才覆盖正式会话文件（绝不冲掉可用会话）。
    """
    def say(*a):
        if not quiet:
            print(*a)

    try:
        cands = harvest_tickets(browser=browser, limit=max(max_try * 3, 20))
    except (FileNotFoundError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    if not cands:
        print(f"✗ {browser} 历史里没有 {b_env.name} 环境的 4A 票据。", file=sys.stderr)
        print("  请先在浏览器里从 4A 入口进一次 B 端后台（进一次就够，票据能用很多天）；"
              "或改用 harvest / paste。", file=sys.stderr)
        return 1

    now = datetime.now()
    say(f"{browser} 历史里找到 {len(cands)} 张 [{b_env.name}] 环境票据（按时间倒序，"
        f"最多试 {max_try} 张）：")
    for i, t in enumerate(cands[:max_try], 1):
        say(f"  {i:>2d}. {t['visitedAt']:%m-%d %H:%M:%S}  距今 {_fmt_age(now - t['visitedAt']):>8s}"
            f"  {t['host']:32s} ticket={t['ticket'][:10]}…")

    if list_only:
        say("（--list：只列候选，未兑换）")
        return 0

    ensure_dir(STATE_DIR)
    trial = STATE_DIR / "b_cookies.renew.json"
    for i, t in enumerate(cands[:max_try], 1):
        trial.unlink(missing_ok=True)          # 每次都从干净状态开始，避免上轮残留
        try:
            BAdminClient(ticket=t["ticket"], cookie_file=trial).login()
        except Exception as e:
            say(f"  [{i:>2d}] ✗ 兑换失败：{str(e)[:120]}")
            continue
        r = probe_session(BAdminClient(cookie_file=trial))
        if not r["ok"]:
            say(f"  [{i:>2d}] ✗ 兑换成功但新会话校验不过：{r.get('error')}")
            continue

        shutil.copy(trial, B_COOKIE_FILE)       # 校验通过才覆盖
        trial.unlink(missing_ok=True)
        h = _load_health()
        h.update({"lastOk": now.isoformat(timespec="seconds"),
                  "userName": r.get("userName"), "userId": r.get("userId"),
                  "source": f"renew:{t['visitedAt']:%Y-%m-%d %H:%M}", "failCount": 0})
        _save_health(h)
        say(f"  [{i:>2d}] ✓ 兑换成功，已落盘；会话可用"
            f"（{r.get('userName')}，userId={r.get('userId')}）")
        return 0

    trial.unlink(missing_ok=True)
    print(f"✗ 试了 {min(max_try, len(cands))} 张票据都没换来可用会话。", file=sys.stderr)
    print("  可能这些票在 4A 侧都过期了 —— 去 4A 门户点一次应用，再跑 renew。",
          file=sys.stderr)
    return 1


# ============================================================================
# 4A CAS 自动化的调研结论
# ============================================================================

CAS_NOTES = """\
【4A 自动登录可行性调研结论 —— 2026-09-29 实测】

链路（生产，需连公司 VPN）：
    https://10.181.58.56:8444/amsportal/                 ← 4A 门户
      302 → http://10.181.58.11:18090/casp/auth/login_mg.jsp?target=...&appCode=IAM000
      302 → https://10.181.58.11:8443/casp/auth/login_mg.jsp?target=...
             （CAS 认证服务；密码表单 POST 到 /casp/loginServlet.do）
    登录成功后，门户里点「赛事票务管理平台」→ 带 ?iamcaspticket= 跳回 B 端。

可自动化的部分：
    · 密码登录表单有 user/passWord 两个字段，且**没有强制图片验证码**
      （页面里 checkCodePic 只在短信登录分支被注释掉，密码分支不校验）。
    · 提交前会调用 IamCyption.encryptSubmit('/casp','loginForm')，
      其实现是 /casp/js/iamcryption.js 里的 jCryption v3.0：
        - 随机生成 AES 密钥 → 用 RSA 公钥加密 → 握手 → 校验 challenge
        - 表单整体用 AES-256 加密后放进单个字段提交
      加密是标准的，理论上可以在 Python 里复刻。

代价与风险：
    · 需要把 **4A 账号密码**放进自动化（企业 SSO 凭据），
      而 4A 前端注释明确写着"用户名密码验证码个人信息明文传输漏洞处理"
      —— 说明贵司安全侧在意这件事。自动化前建议先和运维/安全确认。
    · 密码轮换、策略变更、加 MFA 都会让脚本失效，需要持续维护。
    · 还要额外解决"门户里点哪个应用、应用 URL 是什么"这一步。

因此建议：
    优先用 keepalive 保活（零成本、零风险）；
    保活失效时用 renew 恢复（从浏览器历史捞票，零人工、零授权）；
    harvest / paste 作为兜底；
    只有确实需要"连浏览器都不用开"的无人值守巡检时，再考虑复刻 CAS 登录。
"""


def cmd_cas() -> int:
    print(CAS_NOTES)
    if b_env.four_a_portal:
        print(f"当前环境的 4A 门户：{b_env.four_a_portal}")
    return 0


# ============================================================================
# 命令行
# ============================================================================

def _cli() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m migu_api.bticket",
        description="B 端登录态（4A 票据换来的会话）的获取与保活",
    )
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "keepalive", "ensure", "renew", "harvest",
                             "paste", "cas", "schedule"],
                    help="默认 status")
    ap.add_argument("text", nargs="?", default="",
                    help="paste 用：4A 入口 URL / 票据原文 / hmer 值")
    ap.add_argument("--hmer", action="store_true", help="paste 时按『直接给 hmer 值』处理")
    ap.add_argument("--browser", default="chrome",
                    choices=list(CHROMIUM_BROWSERS), help="renew/harvest 用，默认 chrome")
    ap.add_argument("--no-save", action="store_true",
                    help="harvest 时只校验不落盘")
    ap.add_argument("--max-try", type=int, default=10,
                    help="renew 时最多兑换几张候选票，默认 10")
    ap.add_argument("--list", action="store_true",
                    help="renew 时只列出捞到的候选票据，不兑换")
    ap.add_argument("--no-save-renew", action="store_true",
                    help="与 --list 同义（旧写法兼容）")
    ap.add_argument("--json", action="store_true", help="status 输出 JSON（给脚本用）")
    ap.add_argument("--quiet", action="store_true", help="keepalive/ensure/renew 少打印")
    args = ap.parse_args()

    if args.action == "status":
        return cmd_status(as_json=args.json)
    if args.action == "keepalive":
        return cmd_keepalive(quiet=args.quiet)
    if args.action == "ensure":
        return cmd_ensure(quiet=args.quiet, browser=args.browser)
    if args.action == "renew":
        return cmd_renew(browser=args.browser, max_try=args.max_try,
                         list_only=args.list or args.no_save_renew, quiet=args.quiet)
    if args.action == "harvest":
        return cmd_harvest(browser=args.browser, save=not args.no_save)
    if args.action == "paste":
        if not args.text:
            print("用法：python -m migu_api.bticket paste '<URL 或票据>' [--hmer]",
                  file=sys.stderr)
            return 2
        return cmd_paste(args.text, is_hmer=args.hmer)
    if args.action == "cas":
        return cmd_cas()
    if args.action == "schedule":
        return cmd_schedule()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
