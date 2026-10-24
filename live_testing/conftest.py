# -*- coding: utf-8 -*-
"""conftest.py —— 存活测试的开关、夹具与汇总。

默认**不跑**：仓库全量 pytest / CI 不会因为这里新增 300+ 条网络用例而变慢或变红。
显式打开：
    MIGU_LIVE=1 python -m pytest live_testing -m live -q
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import pytest

from live_testing.cases import load_cases, load_manifest, stale_services
from live_testing.client import LiveClient, ProbeResult
from live_testing.config import LiveSettings, ServiceConfig, load_settings
from live_testing.report import LiveRecorder

_ON = {"1", "true", "yes", "y", "on"}
_GATE_ENV = "MIGU_LIVE"


def live_enabled() -> bool:
    return str(os.environ.get(_GATE_ENV, "")).strip().lower() in _ON


def _targeted(config) -> bool:
    """用户是不是明确点了 live_testing / -m live。"""
    args = [str(arg).replace("\\", "/") for arg in (config.args or [])]
    if any("live_testing" in arg for arg in args):
        return True
    markexpr = str(getattr(config.option, "markexpr", "") or "").lower()
    return "live" in {tok for tok in re.split(r"[^a-z0-9_]+", markexpr) if tok}


def pytest_report_header(config):
    if not live_enabled():
        if _targeted(config):
            return "存活测试：未启用 —— 设置 MIGU_LIVE=1 后重跑"
        return None

    try:
        settings = load_settings()
        cases = load_cases(settings)
        manifest = load_manifest()
    except Exception as exc:  # noqa: BLE001 - 头部只做提示，不吞收集期错误
        return f"存活测试：配置/用例加载失败 —— {exc}"

    counts = ", ".join(f"{name}={len(items)}" for name, items in cases.items())
    lines = [settings.label(), f"存活测试：待执行 {counts}"]
    stale = stale_services(manifest)
    if stale:
        lines.append(
            "⚠ OpenAPI 已变化但用例清单未重新生成："
            f"{', '.join(stale)}；请执行 python -m live_testing.generator"
        )
    if settings.blocked_reason:
        lines.append(f"⚠ {settings.blocked_reason}")
    return "\n".join(lines)


def pytest_collection_modifyitems(config, items):
    if live_enabled():
        return
    live_items = [item for item in items if item.get_closest_marker("live")]
    if not live_items:
        return
    if _targeted(config):
        reason = (
            "存活测试默认不跑：设置 MIGU_LIVE=1 后再执行"
            "（例如 MIGU_LIVE=1 python -m pytest live_testing -m live）"
        )
        for item in live_items:
            item.add_marker(pytest.mark.skip(reason=reason))
    else:
        # 全量跑时直接不收集，避免 300+ 条 skip 噪音。
        items[:] = [item for item in items if item not in live_items]


@dataclass
class ServiceState:
    name: str
    config: ServiceConfig
    client: LiveClient | None
    probe: ProbeResult | None
    skip_reason: str | None = None


def _build_state(name: str, settings: LiveSettings) -> ServiceState:
    cfg = settings.services[name]
    if cfg.skip_reason:
        return ServiceState(name=name, config=cfg, client=None,
                            probe=None, skip_reason=cfg.skip_reason)

    client = LiveClient(cfg, settings)
    probe = client.probe()
    if probe.ok:
        return ServiceState(name=name, config=cfg, client=client, probe=probe)

    if probe.kind == "auth_failed":
        reason = (
            f"{name} 登录态预检失败：{probe.detail}。"
            "请更新 live_config.json 里的 Cookie，或重新 harvest 登录态。"
        )
    elif probe.kind == "unreachable":
        reason = f"{name} 环境不可达：{probe.detail}"
    elif probe.kind == "server_error":
        reason = f"{name} 预检接口 5xx：{probe.detail}"
    else:
        reason = f"{name} 预检返回非预期：{probe.detail}"
    return ServiceState(name=name, config=cfg, client=client,
                        probe=probe, skip_reason=reason)


@pytest.fixture(scope="session")
def live_settings() -> LiveSettings:
    return load_settings()


@pytest.fixture(scope="session")
def live_states(live_settings: LiveSettings) -> dict[str, ServiceState]:
    return {
        name: _build_state(name, live_settings)
        for name in live_settings.selected_services
    }


@pytest.fixture(scope="session")
def customer_state(live_states) -> ServiceState | None:
    return live_states.get("customer")


@pytest.fixture(scope="session")
def web_state(live_states) -> ServiceState | None:
    return live_states.get("web")


@pytest.fixture(scope="session")
def live_recorder(live_settings: LiveSettings, request):
    worker = getattr(request.config, "workerinput", {}).get("workerid", "main")
    recorder = LiveRecorder(live_settings, worker=worker)
    yield recorder
    recorder.write(request.config)
