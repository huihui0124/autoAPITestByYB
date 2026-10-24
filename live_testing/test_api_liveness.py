# -*- coding: utf-8 -*-
"""test_api_liveness.py —— 逐接口存活测试（由 OpenAPI 清单驱动）。

判绿口径（默认）：
    - 网络可达，且 HTTP 状态码 < 500；
    - 业务 code=500 且 message 是「系统错误」等无解释内容 -> 失败；
    - 业务 code=500 但 message 是「不存在/不合法/未找到…」-> 告警（测试数据问题）；
    - 业务 code 的实现细节见 live_testing/classify.py。

跑法：
    MIGU_LIVE=1 python -m pytest live_testing -m live -q
    MIGU_LIVE=1 python -m pytest live_testing -m live -q -k "order"
    MIGU_LIVE=1 MIGU_LIVE_SERVICE=customer python -m pytest live_testing -m live
"""
from __future__ import annotations

import json
import os
import sys

import allure
import pytest

from live_testing.cases import Case, known_issue, load_cases
from live_testing.classify import is_business_500_failure, is_failure
from live_testing.config import load_settings

pytestmark = pytest.mark.live

_SERVICE_LABEL = {"customer": "C 端", "web": "B 端"}
_ON = {"1", "true", "yes", "y", "on"}


def _live_enabled() -> bool:
    return str(os.environ.get("MIGU_LIVE", "")).strip().lower() in _ON


def _targeted_from_argv() -> bool:
    """没有 MIGU_LIVE=1 时，只有明确点了 live_testing / -m live 才加载配置与清单。

    这样一份写坏的 live_config.json 不会把仓库全量 pytest 收集搞挂。
    """
    args = [str(arg).replace("\\", "/") for arg in sys.argv[1:]]
    if any("live_testing" in arg for arg in args):
        return True
    for index, arg in enumerate(args):
        if arg == "-m" and index + 1 < len(args):
            return "live" in args[index + 1].replace("(", " ").replace(")", " ").split()
        if arg.startswith("-m") and len(arg) > 2:
            return "live" in arg[2:].replace("(", " ").replace(")", " ").split()
    return False


_ENABLED = _live_enabled() or _targeted_from_argv()

if _ENABLED:
    _SETTINGS = load_settings()
    try:
        _CASES = load_cases(_SETTINGS)
    except FileNotFoundError as exc:
        pytest.skip(str(exc), allow_module_level=True)

    def _attach(case: Case, result) -> None:
        request_payload = result.request_summary
        allure.attach(
            json.dumps(request_payload, ensure_ascii=False, indent=2, default=str),
            name="请求",
            attachment_type=allure.attachment_type.JSON,
        )
        response_payload = {
            "status": result.status_code,
            "code": result.business_code,
            "message": result.message,
            "content_type": result.content_type,
            "redirected": result.redirected,
            "final_url": result.final_url,
            "elapsed_ms": result.elapsed_ms,
            "fallback_tried": result.fallback_tried,
            "fallback_detail": result.fallback_detail,
            "headers": result.response_headers,
            "body": result.body_preview,
        }
        allure.attach(
            json.dumps(response_payload, ensure_ascii=False, indent=2, default=str),
            name="响应",
            attachment_type=allure.attachment_type.JSON,
        )

    def _labels(case: Case) -> None:
        service = _SERVICE_LABEL.get(case.service, case.service)
        allure.dynamic.feature(f"存活测试-{service}")
        allure.dynamic.story(case.tag)
        allure.dynamic.title(f"{case.method} {case.path}")
        allure.dynamic.severity(allure.severity_level.MINOR)
        if case.operation_id:
            allure.dynamic.parameter("operationId", case.operation_id)

    def _run_case(case: Case, state, recorder) -> None:
        if state is None:
            pytest.skip(f"{case.service} 未被本次运行选中")
        if state.skip_reason:
            pytest.skip(state.skip_reason)

        _labels(case)
        result = state.client.request(case)
        recorder.add(result)
        if _SETTINGS.attach:
            _attach(case, result)

        reason = known_issue(case, _SETTINGS)
        if reason and is_failure(result, _SETTINGS):
            if _SETTINGS.known_issues_mode == "warn":
                # 照常请求、照常进汇总（known_issues 单列），但不让回归变红。
                return
            pytest.xfail(f"已知问题：{reason}；{result.detail()}")

        if result.error:
            pytest.fail(result.detail(), pytrace=False)

        status = result.status_code
        if status is None:  # pragma: no cover - error 分支已覆盖
            pytest.fail(result.detail(), pytrace=False)

        if _SETTINGS.strict_auth and status in {401, 403}:
            pytest.fail(
                f"登录态失效（strict_auth=1）：{result.detail()}",
                pytrace=False,
            )
        if _SETTINGS.fail_on_404 and status == 404:
            pytest.fail(f"接口不存在（fail_on_404=1）：{result.detail()}", pytrace=False)

        assert status < 500, result.detail()
        if is_business_500_failure(result, _SETTINGS):
            pytest.fail(result.detail(), pytrace=False)

    @pytest.mark.parametrize("service", list(_CASES), ids=list(_CASES))
    def test_live_preflight(service: str, live_states) -> None:
        """先确认登录态/环境可用；失败时后续逐接口用例直接 skip，避免刷屏。"""
        state = live_states.get(service)
        allure.dynamic.feature(
            f"存活测试-{_SERVICE_LABEL.get(service, service)}"
        )
        allure.dynamic.story("登录态预检")
        allure.dynamic.title(f"预检 {service} 登录态/环境")
        allure.dynamic.severity(allure.severity_level.BLOCKER)

        if state is None:
            pytest.skip(f"{service} 未被本次运行选中")
        if state.config.skip_reason:
            pytest.skip(state.config.skip_reason)
        if state.probe is None:  # pragma: no cover - _build_state 必然给 probe
            pytest.skip("未执行预检")

        allure.attach(
            state.probe.detail,
            name="预检结果",
            attachment_type=allure.attachment_type.TEXT,
        )
        if state.probe.ok:
            return
        pytest.fail(
            f"{service} 预检未通过（{state.probe.kind}）：{state.probe.detail}\n"
            "请先更新登录态；逐接口用例本轮将全部 skip。",
            pytrace=False,
        )

    if _CASES.get("customer"):
        _CUSTOMER_IDS = [case.id for case in _CASES["customer"]]

        @pytest.mark.parametrize("case", _CASES["customer"], ids=_CUSTOMER_IDS)
        def test_customer_endpoint_alive(case: Case, customer_state,
                                          live_recorder) -> None:
            _run_case(case, customer_state, live_recorder)

    if _CASES.get("web"):
        _WEB_IDS = [case.id for case in _CASES["web"]]

        @pytest.mark.parametrize("case", _CASES["web"], ids=_WEB_IDS)
        def test_web_endpoint_alive(case: Case, web_state, live_recorder) -> None:
            _run_case(case, web_state, live_recorder)
