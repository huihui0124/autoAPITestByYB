# -*- coding: utf-8 -*-
"""config.py —— 存活测试的配置与登录态装载（唯一入口）。

登录态优先级（从高到低）：
    1) 配置文件里的 services.<name>.cookies / .cookie / .headers
    2) 环境变量 MIGU_LIVE_<SERVICE>_COOKIE
    3) 复用仓库已有的 .migu_state/<env>/hmcer.json（C 端）或 b_cookies.json（B 端）

配置文件查找顺序：
    MIGU_LIVE_CONFIG 指定的路径
    ./live_config.json
    ./live_testing/live_config.json

生产环境闸门：
    MIGU_LIVE_ENV=prod 时，必须再设 MIGU_LIVE_ALLOW_PROD=1，否则所有用例 skip。
    （存活测试默认只跑 GET，但生产接口量大，仍要求显式确认。）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = ROOT / ".migu_state"
CATALOG_PATH = ROOT / "catalog.json"
DEFAULT_CONFIG_NAMES = (ROOT / "live_config.json", ROOT / "live_testing" / "live_config.json")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 测试/生产两套前缀。与 migu_api/config.py 的 TEST/PROD、B_TEST/B_PROD 保持一致：
#   C 端生产 storeId=888；B 端生产接口前缀是 /b/api，不是 /api。
# 存活模块刻意不 import migu_api.config：那边会在 import 时按 MIGU_ENV 初始化
# catalog 和登录态，而本模块用 MIGU_LIVE_ENV 独立选环境。改地址时两处一起改。
_PROFILES: dict[str, dict[str, dict]] = {
    "test": {
        "customer": {
            "base_url": "https://union-passport.migu.cn/saishi/api",
            "store_id": 1,
            "verify_ssl": False,
            "no_proxy": False,
            "origin": "https://union-passport.migu.cn",
            "referer": "https://union-passport.migu.cn/saishi/pages/tabs/index?storeId={store_id}",
        },
        "web": {
            "base_url": "http://ticket-test.miguvideo.com:18084/api",
            "store_id": 1,
            "verify_ssl": False,
            "no_proxy": True,
            "origin": "http://ticket-test.miguvideo.com:18084",
            "referer": "http://ticket-test.miguvideo.com:18084/",
        },
    },
    "prod": {
        "customer": {
            "base_url": "https://ticket.miguvideo.com/saishi/api",
            "store_id": 888,
            "verify_ssl": True,
            "no_proxy": False,
            "origin": "https://ticket.miguvideo.com",
            "referer": "https://ticket.miguvideo.com/saishi/pages/tabs/index?storeId={store_id}",
        },
        "web": {
            "base_url": "https://ticket.miguvideo.com/b/api",
            "store_id": 888,
            "verify_ssl": True,
            "no_proxy": True,
            "origin": "https://ticket.miguvideo.com",
            "referer": "https://ticket.miguvideo.com/bfront/",
        },
    },
}

# 预检接口：只用来判断「登录态是否可用 / 环境是否通」，不参与业务断言。
_DEFAULT_PROBES = {
    "customer": {
        "method": "GET",
        "path": "/v1/customer/getInfoV2",
        "query": {"storeId": "{store_id}"},
        "expect_code": 200,
    },
    "web": {
        "method": "GET",
        "path": "/v1/user",
        "query": {},
        "expect_code": 200,
    },
}

_ON = {"1", "true", "yes", "y", "on"}

# 业务 code=500 但 message 命中这些词时，判定为「测试数据/参数问题」而不是服务故障。
# 默认只告警；要一律判失败用 MIGU_LIVE_STRICT_DATA=1。
DEFAULT_BENIGN_500_PATTERNS = (
    "不存在", "不合法", "不支持", "仅支持", "未找到", "找不到", "查不到",
    "查询不到", "没有查询到", "已失效", "已过期", "已删除", "已被删除",
    "未支付", "请检查", "参数错误", "参数有误", "无权限", "登录态已超时",
    # 写接口用空 body 探测时，后端常把“必填校验失败”也包成 code=500。
    # 这些同样是参数/数据问题，不是服务故障。
    "不能为空", "为空", "必填", "请选择", "请先", "未选择", "请传入", "未传",
    "缺少", "未查询到", "无法识别", "认证失败",
    "may not be null", "must not be null",
)


class LiveConfigError(ValueError):
    """配置文件/环境变量写错。"""


@dataclass
class ServiceConfig:
    name: str
    base_url: str
    store_id: int | str = 1
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = False
    no_proxy: bool = False
    timeout: float = 20.0
    retries: int = 1
    auth_probe: dict[str, Any] = field(default_factory=dict)
    param_values: dict[str, Any] = field(default_factory=dict)
    # 按 path（或 "GET /path"）精确覆盖参数：
    # {"GET /v1/address/{id}": {"path": {"id": 123}, "query": {...}, "body": {...}}}
    case_overrides: dict[str, dict] = field(default_factory=dict)
    allow_anonymous: bool = False
    cookie_source: str = "none"     # none / config / env / state
    skip_reason: str | None = None

    @property
    def has_auth(self) -> bool:
        if self.cookies:
            return True
        return any(k.lower() == "authorization" for k in self.headers)


@dataclass
class LiveSettings:
    env: str
    services: dict[str, ServiceConfig]
    selected_services: list[str]
    methods: set[str]
    allow_write: bool
    allow_dangerous: bool
    fail_business_codes: set[int]
    strict_auth: bool
    fail_on_404: bool
    strict_data: bool
    strict_write_business_500: bool
    known_issues_mode: str          # xfail | warn
    benign_500_patterns: tuple[str, ...]
    attach: bool
    delay: float
    body_mode: str
    query_mode: str
    fallback_full_query: bool
    tags_include: set[str]
    tags_exclude: set[str]
    risk_include: set[str]
    risk_exclude: set[str]
    path_include: list[str]
    path_exclude: list[str]
    limit: int
    param_values: dict[str, Any]
    known_issues: dict[str, str]
    config_path: str | None
    blocked_reason: str | None = None

    def label(self) -> str:
        parts = [f"存活测试 env={self.env}",
                 f"methods={','.join(sorted(self.methods)) or '-'}",
                 f"query={self.query_mode}",
                 f"write={'on' if self.allow_write else 'off'}",
                 f"risks={','.join(sorted(self.risk_include)) or 'all'}",
                 f"limit={self.limit or '不限'}"]
        if self.config_path:
            parts.append(f"config={Path(self.config_path).name}")
        for name in self.selected_services:
            cfg = self.services[name]
            if cfg.skip_reason:
                state = "SKIP"
            else:
                state = {
                    "config": "配置",
                    "env": "环境变量",
                    "state": "复用 .migu_state",
                    "none": "无登录态",
                }.get(cfg.cookie_source, cfg.cookie_source)
            parts.append(f"{name}={state}")
        if self.blocked_reason:
            parts.append("生产闸门未开")
        return " ｜ ".join(parts)


# --------------------------------------------------------------------- 基础工具

def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _ON


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    else:
        items = list(value)
    return {str(item).strip() for item in items if str(item).strip()}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    else:
        items = list(value)
    return [str(item).strip() for item in items if str(item).strip()]


_VALID_RISKS = {"read", "safe_write", "write", "dangerous"}


def _valid_risks(value: set[str], source: str) -> set[str]:
    normalized = {item.strip().lower() for item in value if item.strip()}
    invalid = normalized - _VALID_RISKS
    if invalid:
        raise LiveConfigError(
            f"{source} 里有未知风险级别：{', '.join(sorted(invalid))}；"
            f"可选值：{', '.join(sorted(_VALID_RISKS))}"
        )
    return normalized


def _as_codes(value: Any) -> set[int]:
    if value is None:
        return {500}
    codes = set()
    for item in _as_set(value):
        try:
            codes.add(int(item))
        except ValueError:
            continue
    return codes or {500}


def parse_cookie_header(raw: str) -> dict[str, str]:
    """把浏览器里复制出来的 Cookie 串解析成 dict（不依赖 SimpleCookie）。"""
    out: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip().strip('"')
    return out


def _state_cookies(env: str, service: str) -> dict[str, str]:
    """复用仓库已有的登录态缓存；只取 Cookie，不碰其它字段。"""
    state_dir = STATE_ROOT / env
    if service == "customer":
        path = state_dir / "hmcer.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(data, dict):
            return {
                str(k): str(v)
                for k, v in data.items()
                if isinstance(v, (str, int)) and str(v)
            }
        return {}
    else:
        path = state_dir / "b_cookies.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        out: dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    out[str(item["name"])] = str(item.get("value") or "")
        elif isinstance(data, dict):
            out = {str(k): str(v) for k, v in data.items() if v is not None}
        return {k: v for k, v in out.items() if v}


def catalog_seeds(env: str) -> dict[str, Any]:
    """从 catalog.json 取当前环境的测试商品 ID，作为常见路径参数的默认值。

    目的：避免存活测试拿 productId=1 去请求，结果返回“商品不存在”的业务 500。
    只覆盖常见的 ID 名；取值优先级低于 live_config.json 的 param_values。
    """
    if not CATALOG_PATH.exists():
        return {}
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    env_data = (data.get("environments") or {}).get(env) or {}
    projects = env_data.get("projects") or []
    if not isinstance(projects, list) or not projects:
        return {}
    project = next(
        (p for p in projects if isinstance(p, dict) and p.get("status") == "ready"),
        next((p for p in projects if isinstance(p, dict)), None),
    )
    if not project:
        return {}

    seeds: dict[str, Any] = {}
    product_id = project.get("productId")
    if product_id is not None:
        for name in ("productId", "spuId", "projectId", "unifiedProductId",
                     "activityId"):
            seeds[name] = product_id

    events = project.get("events") or []
    if isinstance(events, list) and events:
        event = next((e for e in events if isinstance(e, dict)), None)
        if event:
            event_id = event.get("eventId")
            if event_id is not None:
                seeds["eventId"] = event_id
            skus = event.get("skus") or []
            if isinstance(skus, list) and skus:
                sku = next((s for s in skus if isinstance(s, dict)), None)
                if sku and sku.get("skuId") is not None:
                    seeds["skuId"] = sku["skuId"]
    return {k: v for k, v in seeds.items() if v is not None}


def find_config(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = _env("MIGU_LIVE_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(DEFAULT_CONFIG_NAMES)
    for path in candidates:
        if path.exists():
            return path
    return None


def _load_raw_config(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LiveConfigError(f"读不到存活测试配置：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise LiveConfigError(f"存活测试配置不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise LiveConfigError(f"存活测试配置顶层必须是对象：{path}")
    return data


# --------------------------------------------------------------------- 服务装配

def _build_service(name: str, env: str, raw_service: dict,
                   global_param_values: dict[str, Any],
                   catalog_values: dict[str, Any],
                   global_timeout: float, global_retries: int) -> ServiceConfig:
    profile = _PROFILES[env][name]
    cfg = ServiceConfig(name=name, base_url=str(profile["base_url"]))
    cfg.store_id = profile["store_id"]
    cfg.verify_ssl = bool(profile["verify_ssl"])
    cfg.no_proxy = bool(profile["no_proxy"])
    cfg.timeout = global_timeout
    cfg.retries = global_retries

    # ---- 配置文件覆盖 ----
    if "base_url" in raw_service:
        cfg.base_url = str(raw_service["base_url"]).rstrip("/")
    if "store_id" in raw_service:
        cfg.store_id = raw_service["store_id"]
    if "verify_ssl" in raw_service:
        cfg.verify_ssl = _as_bool(raw_service["verify_ssl"], cfg.verify_ssl)
    if "no_proxy" in raw_service:
        cfg.no_proxy = _as_bool(raw_service["no_proxy"], cfg.no_proxy)
    if "timeout" in raw_service:
        cfg.timeout = _as_float(raw_service["timeout"], cfg.timeout)
    if "retries" in raw_service:
        cfg.retries = max(0, _as_int(raw_service["retries"], cfg.retries))
    if "allow_anonymous" in raw_service:
        cfg.allow_anonymous = _as_bool(raw_service["allow_anonymous"], False)
    if isinstance(raw_service.get("param_values"), dict):
        cfg.param_values.update(raw_service["param_values"])
    if isinstance(raw_service.get("auth_probe"), dict):
        cfg.auth_probe.update(raw_service["auth_probe"])
    raw_overrides = raw_service.get("case_overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise LiveConfigError(f"services.{name}.case_overrides 必须是对象")
    for case_key, sections in raw_overrides.items():
        if not isinstance(sections, dict):
            raise LiveConfigError(
                f"services.{name}.case_overrides.{case_key} 必须是对象"
            )
        for section, values in sections.items():
            if section not in {"path", "query", "body", "cookies"}:
                raise LiveConfigError(
                    f"services.{name}.case_overrides.{case_key} 不支持 {section!r}，"
                    "只能是 path/query/body/cookies"
                )
            if not isinstance(values, dict):
                raise LiveConfigError(
                    f"services.{name}.case_overrides.{case_key}.{section} 必须是对象"
                )
    cfg.case_overrides = raw_overrides

    # ---- 环境变量覆盖 ----
    prefix = f"MIGU_LIVE_{name.upper()}_"
    env_base = _env(prefix + "BASE_URL")
    if env_base:
        cfg.base_url = env_base.rstrip("/")
    env_store = _env(prefix + "STORE_ID")
    if env_store:
        cfg.store_id = env_store
    env_verify = _env(prefix + "VERIFY_SSL")
    if env_verify is not None:
        cfg.verify_ssl = _as_bool(env_verify, cfg.verify_ssl)
    env_noproxy = _env(prefix + "NO_PROXY")
    if env_noproxy is not None:
        cfg.no_proxy = _as_bool(env_noproxy, cfg.no_proxy)
    env_timeout = _env(prefix + "TIMEOUT")
    if env_timeout:
        cfg.timeout = _as_float(env_timeout, cfg.timeout)
    env_retries = _env(prefix + "RETRIES")
    if env_retries is not None:
        cfg.retries = max(0, _as_int(env_retries, cfg.retries))

    raw_headers = raw_service.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise LiveConfigError(f"services.{name}.headers 必须是对象")
    headers = {str(k): str(v) for k, v in raw_headers.items()}
    env_headers = _env(prefix + "HEADERS")
    if env_headers:
        try:
            parsed = json.loads(env_headers)
            if isinstance(parsed, dict):
                headers.update({str(k): str(v) for k, v in parsed.items()})
        except json.JSONDecodeError as exc:
            raise LiveConfigError(
                f"{prefix}HEADERS 必须是 JSON 对象，例如 "
                f'\'{{"Authorization": "Bearer xxx"}}\'（{exc}）'
            ) from exc
    # 允许直接把整段 Cookie 放进 headers.Cookie（大小写不敏感）
    header_cookie = None
    for key in list(headers):
        if key.lower() == "cookie":
            header_cookie = headers.pop(key)
            break

    # ---- 登录态来源 ----
    cookies: dict[str, str] = {}
    source = "none"
    raw_cookies = raw_service.get("cookies")
    raw_cookie = raw_service.get("cookie")
    env_cookie = _env(prefix + "COOKIE")
    if isinstance(raw_cookies, dict) and raw_cookies:
        cookies = {str(k): str(v) for k, v in raw_cookies.items() if v is not None}
        source = "config"
    elif raw_cookie:
        cookies = parse_cookie_header(str(raw_cookie))
        source = "config"
    elif env_cookie:
        cookies = parse_cookie_header(env_cookie)
        source = "env"
    if header_cookie:
        cookies = {**parse_cookie_header(str(header_cookie)), **cookies}
        if source == "none":
            source = "config"
    if not cookies:
        cookies = _state_cookies(env, name)
        source = "state" if cookies else "none"

    # ---- 默认请求头 ----
    origin = str(profile["origin"])
    referer = str(profile["referer"]).format(store_id=cfg.store_id)
    if name == "customer":
        headers.setdefault("X-StoreId", str(cfg.store_id))
    else:
        headers.setdefault("hm-store-id", str(cfg.store_id))
    headers.setdefault("User-Agent", BROWSER_UA)
    headers.setdefault("Accept", "application/json, text/plain, */*")
    headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9")
    headers.setdefault("Referer", referer)
    headers.setdefault("Origin", origin)

    cfg.cookies = cookies
    cfg.headers = headers
    cfg.cookie_source = source

    # 优先级：配置显式 param_values > 全局 param_values > catalog.json 测试商品 ID
    cfg.param_values = {
        **catalog_values,
        **global_param_values,
        **cfg.param_values,
    }

    if not cfg.auth_probe:
        probe = dict(_DEFAULT_PROBES[name])
        probe["query"] = {
            key: (cfg.store_id if value == "{store_id}" else value)
            for key, value in (probe.get("query") or {}).items()
        }
        cfg.auth_probe = probe
    else:
        cfg.auth_probe.setdefault("method", "GET")
        cfg.auth_probe.setdefault("expect_code", 200)
        cfg.auth_probe.setdefault("query", {})

    if not cfg.has_auth and not cfg.allow_anonymous:
        cfg.skip_reason = (
            f"{name} 未配置登录态：在 live_config.json 的 services.{name} 里填 "
            f"cookie/cookies，或设置 MIGU_LIVE_{name.upper()}_COOKIE；"
            "也可以先执行 ./run.sh login --harvest（C 端）/ ./run.sh ticket harvest（B 端）"
            "复用 .migu_state。"
        )
    return cfg


# ------------------------------------------------------------------------- 入口

def load_settings(config_path: str | Path | None = None) -> LiveSettings:
    path = find_config(config_path)
    raw = _load_raw_config(path)

    env = str(_env("MIGU_LIVE_ENV") or raw.get("env") or "test").strip().lower()
    if env not in _PROFILES:
        raise LiveConfigError(
            f"未知 MIGU_LIVE_ENV={env!r}，可选值：{', '.join(_PROFILES)}"
        )

    raw_services = raw.get("services") or {}
    if not isinstance(raw_services, dict):
        raise LiveConfigError("配置里的 services 必须是对象")
    unknown = set(raw_services) - set(_PROFILES[env])
    if unknown:
        raise LiveConfigError(
            f"配置里的 services 有未知端：{', '.join(sorted(unknown))}；"
            f"可选值：{', '.join(_PROFILES[env])}"
        )
    for name, item in raw_services.items():
        if item is not None and not isinstance(item, dict):
            raise LiveConfigError(f"services.{name} 必须是对象")

    global_param_values = raw.get("param_values") or {}
    if not isinstance(global_param_values, dict):
        raise LiveConfigError("配置里的 param_values 必须是对象")

    global_timeout = _as_float(_env("MIGU_LIVE_TIMEOUT") or raw.get("timeout") or 20, 20.0)
    global_retries = max(0, _as_int(_env("MIGU_LIVE_RETRIES") or raw.get("retries") or 1, 1))
    use_catalog_env = _env("MIGU_LIVE_USE_CATALOG")
    use_catalog = (_as_bool(use_catalog_env, True) if use_catalog_env is not None
                   else _as_bool(raw.get("use_catalog"), True))
    catalog_values = catalog_seeds(env) if use_catalog else {}

    services = {
        name: _build_service(
            name, env, raw_services.get(name) or {}, global_param_values,
            catalog_values, global_timeout, global_retries,
        )
        for name in _PROFILES[env]
    }

    methods = _as_set(_env("MIGU_LIVE_METHODS") or raw.get("methods") or ["GET"])
    methods = {m.upper() for m in methods} or {"GET"}
    invalid = methods - {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
    if invalid:
        raise LiveConfigError(f"methods 里有未知 HTTP 方法：{', '.join(sorted(invalid))}")

    selected_raw = _env("MIGU_LIVE_SERVICE") or raw.get("service") or "all"
    if str(selected_raw).strip().lower() in {"all", "*"}:
        selected = list(services)
    else:
        selected = [s.strip().lower() for s in _as_list(selected_raw)]
        missing = [s for s in selected if s not in services]
        if missing:
            raise LiveConfigError(
                f"MIGU_LIVE_SERVICE 里有未知端：{', '.join(missing)}；"
                f"可选值：{', '.join(services)} 或 all"
            )

    body_mode = str(_env("MIGU_LIVE_BODY") or raw.get("body_mode") or "auto").lower()
    if body_mode not in {"auto", "empty", "required"}:
        raise LiveConfigError("MIGU_LIVE_BODY 只能是 auto、empty 或 required")

    query_mode = str(_env("MIGU_LIVE_QUERY") or raw.get("query_mode") or "required").lower()
    if query_mode not in {"required", "all"}:
        raise LiveConfigError("MIGU_LIVE_QUERY 只能是 required 或 all")

    raw_known = raw.get("known_issues") or {}
    if not isinstance(raw_known, dict):
        raise LiveConfigError("配置里的 known_issues 必须是对象")
    known_issues = {str(k): str(v) for k, v in raw_known.items() if v}

    known_issues_mode = str(
        _env("MIGU_LIVE_KNOWN_ISSUES_MODE")
        or raw.get("known_issues_mode")
        or "xfail"
    ).lower()
    if known_issues_mode not in {"xfail", "warn"}:
        raise LiveConfigError("known_issues_mode 只能是 xfail 或 warn")

    limit = _as_int(_env("MIGU_LIVE_LIMIT") or raw.get("limit") or 0, 0)
    attach_env = _env("MIGU_LIVE_ATTACH")
    attach = (_as_bool(attach_env, True) if attach_env is not None
              else _as_bool(raw.get("attach"), True))

    codes_raw = _env("MIGU_LIVE_FAIL_CODES")
    if codes_raw is None:
        codes_raw = raw.get("fail_business_codes")
    if (isinstance(codes_raw, str)
            and codes_raw.strip().lower() in {"none", "off"}):
        fail_business_codes: set[int] = set()
    elif isinstance(codes_raw, (list, tuple)) and not codes_raw:
        fail_business_codes = set()
    else:
        fail_business_codes = _as_codes(codes_raw or [500])

    settings = LiveSettings(
        env=env,
        services=services,
        selected_services=selected,
        methods=methods,
        allow_write=_as_bool(_env("MIGU_LIVE_ALLOW_WRITE") or raw.get("allow_write")),
        allow_dangerous=_as_bool(
            _env("MIGU_LIVE_ALLOW_DANGEROUS") or raw.get("allow_dangerous")),
        fail_business_codes=fail_business_codes,
        strict_auth=_as_bool(_env("MIGU_LIVE_STRICT_AUTH") or raw.get("strict_auth")),
        fail_on_404=_as_bool(_env("MIGU_LIVE_FAIL_ON_404") or raw.get("fail_on_404")),
        strict_data=_as_bool(_env("MIGU_LIVE_STRICT_DATA") or raw.get("strict_data")),
        strict_write_business_500=_as_bool(
            _env("MIGU_LIVE_STRICT_WRITE")
            or raw.get("strict_write_business_500")),
        known_issues_mode=known_issues_mode,
        benign_500_patterns=tuple(
            _as_list(_env("MIGU_LIVE_BENIGN_500_PATTERNS")
                     or raw.get("benign_500_patterns")
                     or list(DEFAULT_BENIGN_500_PATTERNS))
        ) or DEFAULT_BENIGN_500_PATTERNS,
        attach=attach,
        delay=_as_float(_env("MIGU_LIVE_DELAY") or raw.get("delay") or 0, 0.0),
        body_mode=body_mode,
        query_mode=query_mode,
        fallback_full_query=(
            _as_bool(_env("MIGU_LIVE_FALLBACK_QUERY"), True)
            if _env("MIGU_LIVE_FALLBACK_QUERY") is not None
            else _as_bool(raw.get("fallback_full_query"), True)
        ),
        tags_include=_as_set(_env("MIGU_LIVE_TAGS") or raw.get("tags")),
        tags_exclude=_as_set(_env("MIGU_LIVE_SKIP_TAGS") or raw.get("skip_tags")),
        risk_include=_valid_risks(
            _as_set(_env("MIGU_LIVE_RISKS") or raw.get("risks")), "MIGU_LIVE_RISKS"),
        risk_exclude=_valid_risks(
            _as_set(_env("MIGU_LIVE_SKIP_RISKS") or raw.get("skip_risks")),
            "MIGU_LIVE_SKIP_RISKS"),
        path_include=_as_list(_env("MIGU_LIVE_PATH") or raw.get("paths")),
        path_exclude=_as_list(_env("MIGU_LIVE_SKIP_PATH") or raw.get("skip_paths")),
        limit=max(0, limit),
        param_values=dict(global_param_values),
        known_issues=known_issues,
        config_path=str(path) if path else None,
    )

    # 生产闸门：只读也不允许“顺手扫一遍生产”，必须显式确认。
    if env == "prod" and not _as_bool(_env("MIGU_LIVE_ALLOW_PROD")):
        settings.blocked_reason = (
            "MIGU_LIVE_ENV=prod 需要显式设置 MIGU_LIVE_ALLOW_PROD=1 才能执行"
            "（生产接口量大，且必须确认只读口径）。"
        )
        for service in services.values():
            service.skip_reason = settings.blocked_reason

    return settings
