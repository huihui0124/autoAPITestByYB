# -*- coding: utf-8 -*-
"""openapi.py —— 把 OpenAPI 3.x 文档转成「存活用例」字典。

这里不生成 Python 源码，只生成**可审阅、可 diff、可重新生成**的用例清单：
    live_testing/cases/customer.json
    live_testing/cases/web.json

为什么用清单而不是直接拼 500 个 def test_xxx：
    - 500+ 个函数肉眼无法审阅，清单可以；
    - OpenAPI 一刷新，重新跑生成器即可，不需要改代码；
    - pytest 用 parametrize 读清单，Allure 里仍然是「每个接口一条结果」。

参数取值原则（存活测试，不是业务测试）：
    - 必填/可选参数都尽量填一个「看起来合法」的样例值，让请求能进到业务层；
    - body 默认发 {}，靠参数校验返回 4xx —— 4xx 说明路由活着，且不改数据；
    - 需要更深的覆盖时用 MIGU_LIVE_BODY=required 发送必填字段（有写风险，慎用）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options"})
PATH_PARAM_RE = re.compile(r"\{([^{}]+)\}")

# 任何方法都直接判为危险：支付回调、登录回调、注销、把文件重新落 OSS。
# 这些接口即使声明成 GET 也可能产生副作用，默认不打。
ALWAYS_DANGEROUS = re.compile(
    r"(/notify\b|/callback\b|/paySuccess\b|/ssoCallback\b"
    r"|/logout\b|/login\b|/upload/resave\b)",
    re.IGNORECASE,
)

# 写方法（POST/PUT/PATCH/DELETE）命中这些词时，即使打开了写闸门也仍判危险，
# 需要再显式打开 allow_dangerous 才会执行。
WRITE_DANGEROUS = re.compile(
    r"(refund|cancel|close|delete|remove|verification|verify|/pay\b|paySuccess"
    r"|submit|logout|login|notify|callback|reset|clean|clear|batch|import|upload"
    r"|bind|unbind|audit|approve|reject|exchange|redeem|activate|deactivate"
    r"|lock|unlock)",
    re.IGNORECASE,
)

# 非 GET 但本质是查询的接口（Spring 里常用 POST 传复杂查询对象）。
# 这些可以归为 safe_write：MIGU_LIVE_METHODS=GET,POST 时默认执行。
READ_LIKE_OP = re.compile(
    r"^(get|query|list|page|search|find|count|detail|export|download"
    r"|preview|check|validate|calculate|statistics|report|fetch|load|read)",
    re.IGNORECASE,
)
READ_LIKE_PATH = re.compile(
    r"/(query|search|list|page|detail|count|export|download|preview"
    r"|check|validate|calculate|statistics|report)(/|$)",
    re.IGNORECASE,
)

# safe_write 的 body 只填这些“查询条件/分页/店铺”字段，不填业务写字段。
# Spring 的 DTO 往往不标 required，只发 {} 会得到“xxx不能为空”的业务 500。
SAFE_BODY_KEYS = {
    "storeid", "pageindex", "pagesize", "pagenum", "page", "current", "limit",
    "userid", "customerusermanageid", "projectid", "productid", "spuid",
    "eventid", "skuid", "activityid", "unifiedproductid", "channelid",
    "formid", "roleid", "coupontemplateid", "productpushtype", "type", "status",
    "enabled", "enable", "onsale", "authorized", "delflag",
    "coupontypeset", "couponstatusset",
    "starttime", "endtime", "startdate", "enddate",
    "firstonsaletimestart", "firstonsaletimeend",
}

# 已废弃接口：生成阶段直接剔除，避免每次跑都产生无意义的超时/失败。
# 腾讯 LBS（address/location/suggestion/location-image）由业务确认废弃。
DEPRECATED_PATH_PATTERNS = (
    re.compile(r"^/v1/lbs/tencent/", re.IGNORECASE),
)


def is_deprecated_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in DEPRECATED_PATH_PATTERNS)

# header 参数里这些由客户端/会话统一处理，不从 OpenAPI 里生成。
SKIP_HEADER_PARAMS = {
    "x-storeid", "hm-store-id", "content-type", "accept", "accept-language",
    "authorization", "cookie", "user-agent", "referer", "origin", "host",
    "content-length", "connection",
}

# MIGU_LIVE_QUERY=required（默认）时，query 里只保留必填参数 + 这些“低风险且
# 有真实测试数据”的可选参数。像 orderId/ticketId/couponId 这类可选 ID 不再瞎填 1，
# 否则很多接口会返回“订单不存在/系统错误”，把真正的存活问题淹没。
SAFE_QUERY_KEYS = {
    "storeid", "pageindex", "pagesize", "ownertype", "ownerid",
    "productid", "spuid", "eventid", "skuid", "projectid", "activityid",
    "unifiedproductid",
}


class SpecError(ValueError):
    """OpenAPI 文档结构不符合预期。"""


# --------------------------------------------------------------------------- 读取

def load_spec(path: str | Path) -> dict:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"读不到 OpenAPI 文件：{p}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"OpenAPI 不是合法 JSON：{p}（{exc}）") from exc
    if not isinstance(data, dict) or "paths" not in data:
        raise SpecError(f"{p} 不像 OpenAPI 文档：缺少 paths 字段")
    return data


def spec_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def spec_server(spec: dict) -> str:
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict):
        return str(servers[0].get("url") or "")
    return ""


# --------------------------------------------------------------------- $ref 解析

def resolve_pointer(spec: dict, ref: str) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise SpecError(f"只支持文档内 $ref：{ref!r}")
    node: Any = spec
    for raw in ref[2:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise SpecError(f"$ref 无法解析：{ref!r}")
        node = node[part]
    return node


def deref(spec: dict, schema: Any, _depth: int = 0,
          _seen: frozenset[str] = frozenset()) -> dict:
    """把 $ref / allOf / oneOf 解析成可读的 schema（带环保护）。"""
    if _depth > 12 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        ref = str(schema["$ref"])
        if ref in _seen:
            return {}
        try:
            target = resolve_pointer(spec, ref)
        except SpecError:
            return {}
        return deref(spec, target, _depth + 1, _seen | {ref})

    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema.get("allOf") or []:
            sub = deref(spec, part, _depth + 1, _seen)
            merged["properties"].update(sub.get("properties") or {})
            for name in sub.get("required") or []:
                if name not in merged["required"]:
                    merged["required"].append(name)
            for key in ("type", "format", "nullable", "enum", "default", "items"):
                if key in sub and key not in merged:
                    merged[key] = sub[key]
        for key, value in schema.items():
            if key == "allOf":
                continue
            if key == "properties":
                merged["properties"].update(value or {})
            elif key == "required":
                for name in value or []:
                    if name not in merged["required"]:
                        merged["required"].append(name)
            elif key not in merged:
                merged[key] = value
        return merged

    for key in ("oneOf", "anyOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return deref(spec, options[0], _depth + 1, _seen)
    return schema


# ------------------------------------------------------------------- 样例值生成

def _int_sample(name: str, schema: dict) -> int:
    n = (name or "").lower()
    if "pagesize" in n or n in {"size", "limit", "count"}:
        return 10
    if "pageindex" in n or "pageno" in n or "pagenum" in n or n == "page" or n == "current":
        return 1
    if "size" in n:
        return 10
    if "index" in n:
        return 1
    if "storeid" in n:
        return 1
    if "year" in n:
        return 2026
    if "month" in n:
        return 1
    return 1


def _string_sample(name: str, schema: dict) -> str:
    fmt = str(schema.get("format") or "").lower()
    n = (name or "").lower()
    if fmt == "date":
        return "2026-01-01"
    if fmt == "date-time":
        # Spring 的 @DateTimeFormat 默认是 yyyy-MM-dd HH:mm:ss，不是 ISO 的 T 分隔
        return "2026-01-01 00:00:00"
    if fmt == "email":
        return "test@example.com"
    if fmt == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    if fmt in {"binary", "byte"}:
        return ""
    if n in {"from", "to", "start", "end", "begin", "startdate", "enddate"}:
        return "2026-01-01"
    if any(x in n for x in ("phone", "mobile", "tel")):
        return "13800000000"
    if any(x in n for x in ("email", "mail")):
        return "test@example.com"
    if "datetime" in n or "date_time" in n:
        return "2026-01-01 00:00:00"
    if "date" in n:
        return "2026-01-01"
    if "time" in n:
        return "2026-01-01 00:00:00"
    if n.endswith("code") or n in {"code", "verifycode", "captcha", "smscode"}:
        return "1"
    if any(x in n for x in ("url", "link", "redirect", "callback")):
        return "https://example.com"
    if any(x in n for x in ("keyword", "searchkey", "searchword")):
        return ""
    if n in {"name", "nickname", "title", "username"}:
        return "test"
    return "1"


def sample_value(spec: dict, schema: Any, name: str = "", depth: int = 0) -> Any:
    """按 schema 生成一个保守的样例值（不保证业务合法，只保证结构合法）。"""
    schema = deref(spec, schema)
    if not isinstance(schema, dict) or depth > 8:
        return None
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]

    stype = schema.get("type")
    if stype is None:
        if "properties" in schema:
            stype = "object"
        elif "items" in schema:
            stype = "array"
        else:
            return None

    if stype == "integer":
        return _int_sample(name, schema)
    if stype == "number":
        return 1.0 if "price" in (name or "").lower() else 1
    if stype == "boolean":
        return False
    if stype == "string":
        return _string_sample(name, schema)
    if stype == "array":
        items = schema.get("items") or {}
        if not items:
            return []
        sample = sample_value(spec, items, name, depth + 1)
        if sample is None:
            return []
        return [sample]
    if stype == "object":
        if depth >= 4:
            return {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        return {
            key: sample_value(spec, props[key], key, depth + 1)
            for key in required
            if key in props
        }
    return None


def _encode_query_value(value: Any) -> Any:
    """requests 对 bool 会编码成 True/False，Spring 更认小写。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_encode_query_value(v) for v in value if v is not None]
    return value


# --------------------------------------------------------------------- 参数提取

def _operation_parameters(spec: dict, path_item: dict, operation: dict) -> list[dict]:
    raw = list(path_item.get("parameters") or []) + list(operation.get("parameters") or [])
    out: list[dict] = []
    for param in raw:
        if not isinstance(param, dict):
            continue
        if "$ref" in param:
            try:
                param = resolve_pointer(spec, param["$ref"])
            except SpecError:
                continue
        if isinstance(param, dict):
            out.append(param)
    return out


def _flatten_query_object(spec: dict, schema: Any, depth: int = 0,
                          required_only: bool = False) -> dict[str, Any]:
    """Spring @ModelAttribute 在 query 里是**平铺**的：webRequest={...} → pageIndex=1&..."""
    schema = deref(spec, schema)
    if not isinstance(schema, dict) or depth > 3:
        return {}
    props = schema.get("properties") or {}
    required_names = {str(name) for name in (schema.get("required") or [])}
    out: dict[str, Any] = {}
    for prop_name, prop_schema in props.items():
        if (required_only
                and prop_name not in required_names
                and prop_name.lower() not in SAFE_QUERY_KEYS):
            continue
        resolved = deref(spec, prop_schema)
        ptype = resolved.get("type")
        if ptype is None and "properties" in resolved:
            ptype = "object"
        if ptype in {"object", None}:
            # 查询串表达不了嵌套对象，跳过（必填也跳过，4xx 也算活着）。
            continue
        value = sample_value(spec, resolved, prop_name, depth + 1)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        out[prop_name] = _encode_query_value(value)
    return out


def _build_path_params(spec: dict, path: str, params: list[dict]) -> dict[str, Any]:
    definitions = {p.get("name"): p for p in params if p.get("in") == "path"}
    out: dict[str, Any] = {}
    for name in PATH_PARAM_RE.findall(path):
        definition = definitions.get(name) or {}
        value = sample_value(spec, definition.get("schema") or {"type": "string"}, name)
        if value is None or value == "":
            value = 1 if "id" in name.lower() or name.lower().endswith("id") else "1"
        out[name] = value
    return out


def _build_query(spec: dict, params: list[dict],
                 required_only: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for param in params:
        if param.get("in") != "query":
            continue
        name = str(param.get("name") or "")
        if not name:
            continue
        if (required_only
                and not param.get("required")
                and name.lower() not in SAFE_QUERY_KEYS):
            continue
        schema = param.get("schema") or {}
        resolved = deref(spec, schema)
        ptype = resolved.get("type")
        if ptype is None and "properties" in resolved:
            flattened = _flatten_query_object(
                spec, resolved, required_only=required_only)
            out.update(flattened)
            continue
        if ptype == "object" or "properties" in resolved:
            out.update(_flatten_query_object(
                spec, resolved, required_only=required_only))
            continue
        value = sample_value(spec, resolved, name)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        out[name] = _encode_query_value(value)
    return out


def _build_cookies(spec: dict, params: list[dict]) -> dict[str, str]:
    """OpenAPI 里声明的 cookie 参数（例如 tpid）也带上；登录态 Cookie 由客户端覆盖。"""
    out: dict[str, str] = {}
    for param in params:
        if param.get("in") != "cookie":
            continue
        name = str(param.get("name") or "")
        if not name:
            continue
        value = sample_value(spec, param.get("schema") or {"type": "string"}, name)
        if value is None or value == "":
            continue
        out[name] = str(_encode_query_value(value))
    return out


def _build_headers(spec: dict, params: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for param in params:
        if param.get("in") != "header":
            continue
        name = str(param.get("name") or "")
        if not name or name.lower() in SKIP_HEADER_PARAMS:
            continue
        value = sample_value(spec, param.get("schema") or {"type": "string"}, name)
        if value is None:
            continue
        out[name] = str(_encode_query_value(value))
    return out


def _safe_body_sample(spec: dict, schema: Any, depth: int = 0) -> Any:
    """给 safe_write 用的“查询条件”body：只填低风险字段。"""
    schema = deref(spec, schema)
    if not isinstance(schema, dict) or depth > 4:
        return None
    props = schema.get("properties") or {}
    if not props:
        return None
    out: dict[str, Any] = {}
    for name, prop_schema in props.items():
        if name.lower() not in SAFE_BODY_KEYS:
            continue
        resolved = deref(spec, prop_schema)
        ptype = resolved.get("type")
        if ptype is None and "properties" in resolved:
            continue
        if ptype == "object" or "properties" in resolved:
            continue
        value = sample_value(spec, resolved, name, depth + 1)
        if value is not None:
            out[name] = value
    return out or None


def _required_body_sample(spec: dict, schema: Any, depth: int = 0) -> Any:
    """只填 required 字段的 body 样例（MIGU_LIVE_BODY=required 时使用）。"""
    schema = deref(spec, schema)
    if not isinstance(schema, dict) or depth > 5:
        return {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    if not props:
        return sample_value(spec, schema, "", depth)
    out = {}
    for name in required:
        if name not in props:
            continue
        value = sample_value(spec, props[name], name, depth + 1)
        if value is not None:
            out[name] = value
    return out


def _build_body(spec: dict, operation: dict) -> tuple[Any, Any, Any, str | None, bool]:
    """返回 (body_empty, body_required, body_safe, content_type, multipart)。"""
    request_body = operation.get("requestBody") or {}
    content = request_body.get("content") or {}
    if not content:
        return None, None, None, None, False
    if "application/json" in content:
        schema = (content["application/json"] or {}).get("schema") or {}
        return ({}, _required_body_sample(spec, schema),
                _safe_body_sample(spec, schema), "application/json", False)
    if "multipart/form-data" in content:
        schema = (content["multipart/form-data"] or {}).get("schema") or {}
        return (None, _required_body_sample(spec, schema), None,
                "multipart/form-data", True)
    content_type = next(iter(content))
    schema = (content[content_type] or {}).get("schema") or {}
    return {}, _required_body_sample(spec, schema), None, content_type, False


# --------------------------------------------------------------------- 风险分级

def classify_risk(method: str, path: str, operation_id: str = "",
                  multipart: bool = False) -> str:
    """read / safe_write / write / dangerous。

    read        GET/HEAD/OPTIONS
    safe_write  非 GET，但 operationId/路径表明是查询（query/list/count/...）
    write       其它非 GET，需要 MIGU_LIVE_ALLOW_WRITE=1
    dangerous   支付/退款/核销/删除/登录/回调/批量等，需要 MIGU_LIVE_ALLOW_DANGEROUS=1
    """
    method = method.upper()
    if ALWAYS_DANGEROUS.search(path):
        return "dangerous"
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "read"
    if method == "DELETE" or WRITE_DANGEROUS.search(path):
        return "dangerous"
    if multipart:
        # 文件上传/导入不是纯查询，交给 MIGU_LIVE_ALLOW_WRITE 控制
        return "write"
    if READ_LIKE_OP.match(operation_id or "") or READ_LIKE_PATH.search(path):
        return "safe_write"
    return "write"


def iter_operations(spec: dict) -> Iterator[tuple[str, str, dict, dict]]:
    """yield (method, path, path_item, operation)，跳过 deprecated。"""
    for path, path_item in (spec.get("paths") or {}).items():
        if is_deprecated_path(path):
            continue
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            if operation.get("deprecated"):
                continue
            yield method.upper(), path, path_item, operation


# --------------------------------------------------------------------- 用例构建

def build_case(spec: dict, service: str, method: str, path: str,
               path_item: dict, operation: dict) -> dict:
    params = _operation_parameters(spec, path_item, operation)
    body, body_required, body_safe, content_type, multipart = _build_body(spec, operation)
    tags = operation.get("tags") or []
    tag = str(tags[0]) if tags else "untagged"
    operation_id = str(operation.get("operationId") or "")
    summary = str(operation.get("summary") or "")
    return {
        "id": f"{method} {path}",
        "service": service,
        "method": method,
        "path": path,
        "tag": tag,
        "operation_id": operation_id,
        "summary": summary,
        "path_params": _build_path_params(spec, path, params),
        "query": _build_query(spec, params),
        "query_required": _build_query(spec, params, required_only=True),
        "cookies": _build_cookies(spec, params),
        "headers": _build_headers(spec, params),
        "body": body,
        "body_required": body_required,
        "body_safe": body_safe,
        "body_content_type": content_type,
        "multipart": multipart,
        "risk": classify_risk(method, path, operation_id, multipart),
    }


def build_cases(spec_path: str | Path, service: str) -> tuple[list[dict], dict]:
    spec = load_spec(spec_path)
    cases = [
        build_case(spec, service, method, path, path_item, operation)
        for method, path, path_item, operation in iter_operations(spec)
    ]
    cases.sort(key=lambda c: (c["path"], c["method"]))
    return cases, build_manifest(spec_path, service, spec, cases)


def build_manifest(spec_path: str | Path, service: str, spec: dict,
                   cases: list[dict]) -> dict:
    by_method: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    tags: dict[str, int] = {}
    for case in cases:
        by_method[case["method"]] = by_method.get(case["method"], 0) + 1
        by_risk[case["risk"]] = by_risk.get(case["risk"], 0) + 1
        tags[case["tag"]] = tags.get(case["tag"], 0) + 1
    source_path = Path(spec_path)
    try:
        source = source_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        source = str(source_path)

    return {
        "service": service,
        "source": source,
        "source_sha256": spec_sha256(spec_path),
        "openapi": spec.get("openapi"),
        "server": spec_server(spec),
        "deprecated_skipped": sorted(
            path for path in (spec.get("paths") or {}) if is_deprecated_path(path)
        ),
        "counts": {
            "total": len(cases),
            "by_method": dict(sorted(by_method.items())),
            "by_risk": dict(sorted(by_risk.items())),
            "by_tag": dict(sorted(tags.items())),
        },
    }
