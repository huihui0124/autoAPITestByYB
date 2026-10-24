# -*- coding: utf-8 -*-
"""client.py —— 存活测试专用的轻量 HTTP 客户端。

和仓库里的 MiguClient / BAdminClient 有什么区别：
    - MiguClient/BAdminClient 面向**业务用例**：code != 200 直接抛异常，适合断言；
    - 本客户端面向**存活探测**：保留原始 HTTP 状态 + 业务 code，4xx 也算“活着”，
      只把 5xx / 网络错误 / 配置的 5xx 业务码判失败。

登录态只负责“带上”，不负责“登录/保活”。取 Cookie 的逻辑在 config.py，
失效时会由预检用例给出明确提示。
"""
from __future__ import annotations

import time
import urllib3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from .cases import Case
from .classify import is_benign_data_5xx
from .config import LiveSettings, ServiceConfig
from .openapi import PATH_PARAM_RE

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SENSITIVE_HEADERS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "hmcer", "hmer", "hmctoken", "token", "x-token", "iamcaspticket",
}
_SENSITIVE_KEYS = {
    "token", "access_token", "authorization", "ticket", "iamcaspticket",
    "hmcer", "hmer", "hmctoken", "password", "verifycode",
}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class LiveResult:
    service: str
    method: str
    path: str
    risk: str = ""
    url: str = ""
    status_code: int | None = None
    business_code: Any = None
    message: str = ""
    body_preview: str = ""
    content_type: str = ""
    elapsed_ms: float = 0.0
    error: str | None = None
    redirected: bool = False
    final_url: str = ""
    request_summary: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)
    fallback_tried: bool = False
    fallback_detail: str = ""

    @property
    def case_id(self) -> str:
        return f"{self.method} {self.path}"

    def detail(self) -> str:
        """给 pytest 失败信息用的一行/多行摘要。"""
        if self.error:
            return f"{self.method} {self.path} -> 网络错误：{self.error}（{self.url}）"
        parts = [f"{self.method} {self.path} -> HTTP {self.status_code}"]
        if self.business_code is not None:
            parts.append(f"code={self.business_code}")
        if self.message:
            parts.append(f"message={self.message}")
        if self.url:
            parts.append(f"url={self.url}")
        if self.body_preview:
            parts.append(f"body={self.body_preview[:500]}")
        return " ｜ ".join(parts)


@dataclass
class ProbeResult:
    kind: str            # ok / no_credentials / auth_failed / server_error / unexpected / unreachable
    detail: str
    result: LiveResult | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _mask_headers(headers: dict) -> dict:
    return {
        key: ("***" if str(key).lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


def _mask_values(mapping: dict) -> dict:
    return {
        key: ("***" if str(key).lower() in _SENSITIVE_KEYS else value)
        for key, value in mapping.items()
    }


class LiveClient:
    """一个端（customer/web）一个实例；session 复用到进程结束。"""

    def __init__(self, config: ServiceConfig, settings: LiveSettings):
        self.cfg = config
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(config.headers)
        for name, value in config.cookies.items():
            # 不指定 domain：这些 Cookie 只可能发往本端的 base_url
            self.session.cookies.set(name, value)
        self.session.verify = config.verify_ssl
        if config.no_proxy:
            self.session.trust_env = False
            self.session.proxies = {"http": None, "https": None}

    # ------------------------------------------------------------------ 参数

    def _param(self, name: str, default: Any, overrides: dict | None = None) -> Any:
        if name.lower() == "storeid":
            return self.cfg.store_id
        if overrides and name in overrides:
            return overrides[name]
        if name in self.cfg.param_values:
            return self.cfg.param_values[name]
        return default

    def _override(self, case: Case, section: str) -> dict:
        """按 "GET /path" 或 "/path" 取 case_overrides.<section>。"""
        for key in (case.id, case.path):
            sections = self.cfg.case_overrides.get(key)
            if isinstance(sections, dict):
                values = sections.get(section)
                if isinstance(values, dict):
                    return values
        return {}

    def _build_path(self, case: Case) -> str:
        overrides = self._override(case, "path")

        def replace(match):
            name = match.group(1)
            value = self._param(name, case.path_params.get(name, 1), overrides)
            return quote(str(value), safe="")

        path = PATH_PARAM_RE.sub(replace, case.path)
        return path if path.startswith("/") else "/" + path

    def _build_query(self, case: Case, force_all: bool = False) -> dict:
        source = case.query
        if (not force_all
                and self.settings.query_mode == "required"
                and case.query_required is not None):
            source = case.query_required
        overrides = self._override(case, "query")
        out: dict[str, Any] = {}
        for name, value in source.items():
            value = self._param(name, value, overrides)
            if value is None:
                continue
            out[name] = value
        # 接口文档没声明、但后端实际必填的 query（例如 storeId），通过 override 补上。
        existing = {name.lower() for name in out}
        for name, value in overrides.items():
            if name.lower() not in existing and value is not None:
                out[name] = value
        return out

    def _build_cookies(self, case: Case) -> dict:
        overrides = self._override(case, "cookies")
        out: dict[str, Any] = {}
        for name, value in case.cookies.items():
            value = self._param(name, value, overrides)
            if value in (None, ""):
                continue
            out[name] = value
        # 登录态 Cookie 优先级最高
        return {**out, **self.cfg.cookies}

    def _apply_body_params(self, value: Any, overrides: dict) -> Any:
        """把 param_values / case_overrides.body 应用到 JSON body 的字段名上。"""
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if key in overrides:
                    out[key] = overrides[key]
                elif key.lower() == "storeid":
                    out[key] = self.cfg.store_id
                elif key in self.cfg.param_values:
                    out[key] = self.cfg.param_values[key]
                else:
                    out[key] = self._apply_body_params(item, overrides)
            for key, item in overrides.items():
                out.setdefault(key, item)
            return out
        if isinstance(value, list):
            return [self._apply_body_params(item, overrides) for item in value]
        return value

    def _build_headers(self, case: Case) -> dict:
        # 用例级 header（OpenAPI 里声明的）优先让位给运行期配置（UA/Referer/Cookie 等）
        return {**case.headers, **self.cfg.headers}

    def _body_kwargs(self, case: Case) -> dict:
        method = case.method.upper()
        if method in _SAFE_METHODS or not case.body_content_type:
            return {}
        body = case.body
        if self.settings.body_mode == "required" and case.body_required is not None:
            body = case.body_required
        elif (self.settings.body_mode == "auto"
              and case.risk == "safe_write"
              and case.body_safe is not None):
            body = case.body_safe
        body_overrides = self._override(case, "body")
        if body is not None:
            body = self._apply_body_params(body, body_overrides)
        elif body_overrides:
            body = dict(body_overrides)
        if case.multipart:
            return {"files": {}, "data": body or {}}
        if case.body_content_type == "application/json":
            return {"json": body if body is not None else {}}
        return {"data": body if body is not None else {}}

    # ------------------------------------------------------------------ 请求

    def request(self, case: Case) -> LiveResult:
        """发请求。

        query_mode=required（默认）时先只发必填 + 安全可选参数；
        如果返回业务 500，再用全量 query 试一次 —— OpenAPI 里不少“必填”标错，
        只发 required 反而会让接口拿不到 ownerType/startTime 之类的关键参数。
        """
        minimal = (self.settings.query_mode == "required"
                   and case.query_required is not None)
        query = self._build_query(case, force_all=not minimal)
        result = self._request_once(case, query)

        if not (minimal
                and self.settings.fallback_full_query
                and case.method.upper() in _SAFE_METHODS
                and self._looks_bad(result)):
            return result

        full_query = self._build_query(case, force_all=True)
        if full_query == query:
            return result

        alt = self._request_once(case, full_query)
        alt.fallback_tried = True
        result.fallback_tried = True
        if self._rank(alt) < self._rank(result):
            alt.fallback_detail = "required 参数返回业务 500，全量 query 后恢复"
            return alt
        result.fallback_detail = "required 参数返回业务 500，全量 query 重试未改善"
        return result

    def _looks_bad(self, result: LiveResult) -> bool:
        if result.error or result.status_code is None:
            return True
        if result.status_code >= 500:
            return True
        return (result.business_code in self.settings.fail_business_codes
                and not is_benign_data_5xx(result, self.settings))

    def _rank(self, result: LiveResult) -> int:
        """0=正常，1=数据类业务 500，2=业务 500，3=HTTP 5xx/网络错误。"""
        if result.error or result.status_code is None:
            return 3
        if result.status_code >= 500:
            return 3
        if result.business_code in self.settings.fail_business_codes:
            return 1 if is_benign_data_5xx(result, self.settings) else 2
        return 0

    def _request_once(self, case: Case, query: dict) -> LiveResult:
        path = self._build_path(case)
        url = f"{self.cfg.base_url.rstrip('/')}{path}"
        cookies = self._build_cookies(case)
        headers = self._build_headers(case)
        body_kwargs = self._body_kwargs(case)
        if case.multipart:
            headers.pop("Content-Type", None)

        request_summary = {
            "method": case.method,
            "url": url,
            "path_params": _mask_values(case.path_params),
            "query": _mask_values(query),
            "cookies": {name: "***" for name in cookies},
            "headers": _mask_headers(headers),
            "body": body_kwargs.get("json", body_kwargs.get("data")),
            "content_type": case.body_content_type,
        }

        if self.settings.delay > 0:
            time.sleep(self.settings.delay)

        attempts = 1 + max(0, self.cfg.retries)
        last_error: Exception | None = None
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = self.session.request(
                    case.method,
                    url,
                    params=query,
                    cookies=cookies,
                    headers=headers,
                    timeout=self.cfg.timeout,
                    allow_redirects=True,
                    **body_kwargs,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt < attempts - 1 and case.method.upper() in _SAFE_METHODS:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return LiveResult(
                    service=case.service,
                    method=case.method,
                    path=case.path,
                    risk=case.risk,
                    url=url,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    request_summary=request_summary,
                )

            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            if (response.status_code in {502, 503, 504}
                    and attempt < attempts - 1
                    and case.method.upper() in _SAFE_METHODS):
                response.close()
                time.sleep(0.5 * (attempt + 1))
                continue

            return self._to_result(case, url, response, elapsed_ms, request_summary)

        return LiveResult(  # pragma: no cover - 循环必然 return
            service=case.service,
            method=case.method,
            path=case.path,
            risk=case.risk,
            url=url,
            error=f"重试耗尽：{last_error}",
            request_summary=request_summary,
        )

    @staticmethod
    def _to_result(case: Case, url: str, response: requests.Response,
                   elapsed_ms: float, request_summary: dict) -> LiveResult:
        try:
            body = response.json()
        except ValueError:
            body = None

        business_code = body.get("code") if isinstance(body, dict) else None
        if isinstance(business_code, str):
            try:
                business_code = int(business_code)
            except ValueError:
                pass
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("msg") or "")

        return LiveResult(
            service=case.service,
            method=case.method,
            path=case.path,
            risk=case.risk,
            url=url,
            status_code=response.status_code,
            business_code=business_code,
            message=message,
            body_preview=(response.text or "")[:2000],
            content_type=str(response.headers.get("Content-Type") or ""),
            elapsed_ms=elapsed_ms,
            redirected=bool(response.history),
            final_url=str(response.url),
            request_summary=request_summary,
            response_headers=_mask_headers(dict(response.headers)),
        )

    # ------------------------------------------------------------------ 预检

    def probe(self) -> ProbeResult:
        """用配置里的 auth_probe 判断登录态/环境是否可用。

        返回 kind：
            ok             预检通过
            auth_failed    登录态失效（HTTP 401/403 或业务 code=401）
            server_error   预检接口 5xx / 配置的 5xx 业务码
            unreachable    网络不可达
            unexpected     其它非预期（预检接口 404、业务码不是 expect_code 等）
        """
        probe_cfg = self.cfg.auth_probe or {}
        method = str(probe_cfg.get("method") or "GET").upper()
        path = str(probe_cfg.get("path") or "/")
        query = probe_cfg.get("query") or {}
        expect = probe_cfg.get("expect_code", 200)
        if expect is None:
            expect_codes = None
        elif isinstance(expect, (list, tuple, set)):
            expect_codes = set(expect)
        else:
            expect_codes = {expect}

        case = Case(
            id=f"PROBE {method} {path}",
            service=self.cfg.name,
            method=method,
            path=path,
            tag="__preflight__",
            query=dict(query),
        )
        result = self.request(case)

        if result.error:
            return ProbeResult("unreachable", result.error, result)
        if result.status_code in {401, 403} or result.business_code == 401:
            return ProbeResult(
                "auth_failed",
                f"预检 {method} {path} -> HTTP {result.status_code}, "
                f"code={result.business_code}, message={result.message or '-'}",
                result,
            )
        if (result.status_code is not None and result.status_code >= 500) \
                or result.business_code in self.settings.fail_business_codes:
            return ProbeResult("server_error", result.detail(), result)
        if result.business_code is None:
            if expect_codes is None:
                return ProbeResult("ok", result.detail(), result)
            return ProbeResult(
                "unexpected",
                "预检接口没有返回业务 code（可能被重定向到登录页或返回了 HTML）："
                + result.detail(),
                result,
            )
        if expect_codes is not None and result.business_code not in expect_codes:
            return ProbeResult("unexpected", result.detail(), result)
        return ProbeResult("ok", result.detail(), result)
