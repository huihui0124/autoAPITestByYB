# -*- coding: utf-8 -*-
"""cases.py —— 读取生成器产出的用例清单，并按运行期开关过滤。

过滤维度（环境变量）：
    MIGU_LIVE_SERVICE          customer / web / all
    MIGU_LIVE_METHODS          GET（默认）/ GET,POST ...
    MIGU_LIVE_ALLOW_DANGEROUS  1 才允许 risk=dangerous 的接口
    MIGU_LIVE_TAGS             只跑这些 tag（逗号分隔）
    MIGU_LIVE_SKIP_TAGS        跳过这些 tag
    MIGU_LIVE_PATH             只跑 path 包含这些子串的接口
    MIGU_LIVE_SKIP_PATH        跳过 path 包含这些子串的接口
    MIGU_LIVE_LIMIT            每个端最多跑多少条（0=不限）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ROOT, LiveSettings, _env

# 登录/注销/支付回调类即使标成 dangerous，也放到最后跑，避免中途把会话打掉。
_SESSION_MUTATING = re.compile(
    r"(/login\b|/logout\b|/ssoCallback\b|/notify\b|/callback\b|/paySuccess\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Case:
    id: str
    service: str
    method: str
    path: str
    tag: str
    operation_id: str = ""
    summary: str = ""
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    query_required: dict[str, Any] | None = None
    cookies: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    body_required: Any = None
    body_safe: Any = None
    body_content_type: str | None = None
    multipart: bool = False
    risk: str = "read"

    @classmethod
    def from_dict(cls, data: dict) -> "Case":
        return cls(
            id=str(data.get("id") or f"{data.get('method')} {data.get('path')}"),
            service=str(data.get("service") or ""),
            method=str(data.get("method") or "GET").upper(),
            path=str(data.get("path") or "/"),
            tag=str(data.get("tag") or "untagged"),
            operation_id=str(data.get("operation_id") or ""),
            summary=str(data.get("summary") or ""),
            path_params=dict(data.get("path_params") or {}),
            query=dict(data.get("query") or {}),
            query_required=(dict(data["query_required"])
                            if data.get("query_required") is not None else None),
            cookies=dict(data.get("cookies") or {}),
            headers=dict(data.get("headers") or {}),
            body=data.get("body"),
            body_required=data.get("body_required"),
            body_safe=data.get("body_safe"),
            body_content_type=data.get("body_content_type"),
            multipart=bool(data.get("multipart")),
            risk=str(data.get("risk") or "read"),
        )


def cases_dir() -> Path:
    raw = _env("MIGU_LIVE_CASES")
    return Path(raw) if raw else ROOT / "live_testing" / "cases"


def load_manifest() -> dict:
    path = cases_dir() / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"找不到存活用例清单：{path}\n"
            "先执行：python -m live_testing.generator"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load_service_cases(service: str) -> list[Case]:
    path = cases_dir() / f"{service}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {service} 的存活用例：{path}\n"
            "先执行：python -m live_testing.generator"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} 顶层必须是数组")
    return [Case.from_dict(item) for item in raw if isinstance(item, dict)]


def _keep(case: Case, settings: LiveSettings) -> bool:
    if case.method not in settings.methods:
        return False
    if settings.risk_include and case.risk not in settings.risk_include:
        return False
    if case.risk in settings.risk_exclude:
        return False
    if case.risk == "write" and not settings.allow_write:
        return False
    if case.risk == "dangerous" and not settings.allow_dangerous:
        return False

    tag = case.tag.lower()
    include_tags = {t.lower() for t in settings.tags_include}
    exclude_tags = {t.lower() for t in settings.tags_exclude}
    if include_tags and tag not in include_tags:
        return False
    if tag in exclude_tags:
        return False

    if settings.path_include and not any(s in case.path for s in settings.path_include):
        return False
    if any(s in case.path for s in settings.path_exclude):
        return False
    return True


def load_cases(settings: LiveSettings) -> dict[str, list[Case]]:
    """返回 {service: [Case, ...]}；只包含本次会被收集的接口。"""
    result: dict[str, list[Case]] = {}
    for service in settings.selected_services:
        kept = [case for case in _load_service_cases(service) if _keep(case, settings)]
        kept.sort(key=lambda case: (
            1 if _SESSION_MUTATING.search(case.path) else 0,
            case.path,
            case.method,
        ))
        if settings.limit:
            kept = kept[:settings.limit]
        result[service] = kept
    return result


def known_issue(case: Case, settings: LiveSettings) -> str | None:
    """按 "GET /path" 或 "/path" 查已知问题；命中后 pytest 里标 xfail。"""
    for key in (case.id, case.path):
        reason = settings.known_issues.get(key)
        if reason:
            return reason
    return None


def stale_services(manifest: dict) -> list[str]:
    """OpenAPI 改过但用例清单没重新生成时，返回过期的端名。"""
    from .openapi import spec_sha256

    stale: list[str] = []
    for service, info in (manifest.get("services") or {}).items():
        source = info.get("source")
        expected = info.get("source_sha256")
        if not source or not expected:
            continue
        path = Path(source)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            continue
        try:
            if spec_sha256(path) != expected:
                stale.append(service)
        except OSError:
            continue
    return stale
