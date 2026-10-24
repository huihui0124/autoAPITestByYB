# -*- coding: utf-8 -*-
"""classify.py —— 存活结果的分类与“是否算失败”。

为什么要区分 business_5xx 和 business_data_5xx：
    存活测试用样例值（productId=222945、orderId=1…）去打接口，
    很多写得很规范的接口会返回 `{"code":500,"message":"订单不存在"}` ——
    这**说明路由和服务都活着**，只是测试数据不存在。
    真正需要关注的是 `{"code":500,"message":"系统错误"}` 这类没有业务解释的 500。

默认口径：
    - HTTP 5xx / 网络错误              -> 失败
    - 业务 code=500 且 message 命中
      benign_500_patterns（不存在等）  -> 告警（business_data_5xx），不失败
    - 业务 code=500 且 message 未命中  -> 失败（business_5xx）
    - MIGU_LIVE_STRICT_DATA=1          -> 把 business_data_5xx 也判失败
    - MIGU_LIVE_FAIL_CODES=500,...     -> 扩展/收窄要判失败的业务码
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .config import LiveSettings

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示，避免 client <-> classify 循环引用
    from .client import LiveResult


def is_benign_data_5xx(result: "LiveResult", settings: LiveSettings) -> bool:
    """业务码是要判失败的 5xx，但 message 表明是“数据/参数不存在”。"""
    if result.business_code not in settings.fail_business_codes:
        return False
    message = result.message or ""
    if not message:
        return False
    return any(pattern and pattern in message
               for pattern in settings.benign_500_patterns)


def is_business_500_failure(result: "LiveResult", settings: LiveSettings) -> bool:
    """业务 5xx 是否算失败。

    - read / safe_write：算失败（除非 message 属于数据/参数不存在）
    - write / dangerous：空 body 探测产生的业务 500 默认只告警，因为这不是
      “接口不可达”，而是缺业务参数；需要严格时设 MIGU_LIVE_STRICT_WRITE=1。
    """
    if result.business_code not in settings.fail_business_codes:
        return False
    if is_benign_data_5xx(result, settings):
        return False
    if (result.risk in {"write", "dangerous"}
            and not settings.strict_write_business_500):
        return False
    return True


def category(result: "LiveResult", settings: LiveSettings) -> str:
    if result.error or result.status_code is None:
        return "network_error"
    if result.status_code >= 500:
        return "server_5xx"
    if result.business_code in settings.fail_business_codes:
        if is_benign_data_5xx(result, settings):
            return "business_data_5xx"
        if (result.risk in {"write", "dangerous"}
                and not settings.strict_write_business_500):
            return "business_write_5xx"
        return "business_5xx"
    if result.status_code in {401, 403}:
        return "auth_4xx"
    if result.status_code == 404:
        return "not_found_404"
    if result.status_code >= 400:
        return "client_4xx"
    if result.status_code >= 300:
        return "redirect_3xx"
    if "text/html" in result.content_type.lower():
        return "html_2xx"
    return "ok_2xx"


def is_failure(result: "LiveResult", settings: LiveSettings) -> bool:
    if result.error or result.status_code is None:
        return True
    if result.status_code >= 500:
        return True
    if is_business_500_failure(result, settings):
        return True
    if settings.strict_auth and result.status_code in {401, 403}:
        return True
    if settings.fail_on_404 and result.status_code == 404:
        return True
    return False


def is_failure_from_row(row: dict, settings: LiveSettings) -> bool:
    """给汇总 JSON 用（row 是已经序列化的结果）。"""
    if row.get("known_issue"):
        return False
    cat = row.get("category")
    if cat in {"network_error", "server_5xx", "business_5xx"}:
        return True
    if cat == "business_write_5xx" and settings.strict_write_business_500:
        return True
    if cat == "business_data_5xx" and settings.strict_data:
        return True
    if settings.strict_auth and cat == "auth_4xx":
        return True
    if settings.fail_on_404 and cat == "not_found_404":
        return True
    return False
