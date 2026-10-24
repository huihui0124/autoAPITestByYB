# -*- coding: utf-8 -*-
"""
fundgate.py —— 「真实资金动作」闸门（被 skip 掉的那一类用例，运行时决定跑不跑）

问题：
    回归里有一组用例必须**花真钱 / 真人扫码**才能跑：
      · 真实支付闭环（test_real_pay.py）：下单 → 人工付款 → 出票 → 核销
      · 生产的部分退（test_b_refund.py）：必须有一笔**付费**多份单才能暴露
        「退部分份数却把整单结清」那个 bug（0 元单不复现，见 config.py 注释）
    它们既不能默认跑（会花钱），也不该写死 `pytest.skip`（等于永远没覆盖）。
    所以做成一个**运行时可选**的闸门：跑之前一句话决定，不用改代码。

三种模式（`--real-pay=off|auto|on`，等价环境变量 `MIGU_REAL_PAY=0|auto|1`）：
    off   默认。要花钱的用例全部跳过；门店里现成的可复用订单也不碰。
    auto  不花新钱。只使用**已经付过款**的订单：
          `MIGU_PAY_ORDER=<orderId>`（真实支付续跑）、
          `MIGU_REFUND_PARTIAL_ORDER=<orderId>`（部分退指定单），
          或门店里现成可用的多份可退单。没有就跳过。
    on    允许**下新单 + 等真人扫码**，把真实资金链路全部跑齐：
          用例会自己下单、打印/附上二维码、阻塞等待你付款，然后接着跑。
          ⚠️ 真金花费：测试环境一轮 0.03 元起；生产一轮最多 0.04 元
          （部分退多份单 0.03 + 真实支付闭环单 0.01）。

优先级：pytest 命令行 `--real-pay` > 环境变量 `MIGU_REAL_PAY` > 默认 off。
（conftest.py 会把命令行选项写回环境变量，所以各模块 import 时读到的就是最终值。）

用法：
    ./run.sh all --real-pay=on            # 命令行最省事
    ./.venv/bin/pytest --real-pay=auto    # 直接用已付款订单
    MIGU_REAL_PAY=1 ./.venv/bin/pytest    # 环境变量等价写法
"""
from __future__ import annotations

import os

OFF = "off"
AUTO = "auto"
ON = "on"
CHOICES = (OFF, AUTO, ON)

ENV_VAR = "MIGU_REAL_PAY"
# 指定了已付款订单 = 明确要跑那条链路，且**不花新钱** → 至少 auto
RESUME_VARS = ("MIGU_PAY_ORDER", "MIGU_REFUND_PARTIAL_ORDER")

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"", "0", "false", "no", "n", "off"}

_resolved: str | None = None


def resolve(raw: str | None) -> str:
    """把外部取值（命令行 / 环境变量）规范化成 off|auto|on。"""
    token = (raw or "").strip().lower()
    if token in _TRUTHY:
        return ON
    if token == AUTO:
        return AUTO
    if token in _FALSY:
        return OFF
    raise ValueError(
        f"真实支付闸门取值非法：{raw!r}，可选 {CHOICES}"
        f"（环境变量 {ENV_VAR}，命令行 --real-pay）"
    )


def set_mode(value: str | None) -> str:
    """显式设置并缓存模式（conftest 把 `--real-pay` 塞进来时用）。"""
    global _resolved
    _resolved = resolve(value) if value is not None else None
    return mode()


def mode() -> str:
    """当前模式。读一次后缓存（同一进程内不会变）。"""
    global _resolved
    if _resolved is None:
        _resolved = resolve(os.environ.get(ENV_VAR))
        if _resolved == OFF and any(os.environ.get(v) for v in RESUME_VARS):
            _resolved = AUTO
    return _resolved


def allows_existing() -> bool:
    """能否使用"已经付过款"的订单（auto / on）。"""
    return mode() in (AUTO, ON)


def allows_new_payment() -> bool:
    """能否**下新单并等真人付款**（只有 on）。"""
    return mode() == ON


def pay_timeout() -> int:
    """等人工付款的最长秒数。默认按环境给：测试收银台 ~118s，生产 ~899s。"""
    raw = os.environ.get("MIGU_PAY_TIMEOUT")
    if raw:
        return int(raw)
    from migu_api.config import env
    return 600 if env.name == "prod" else 110


def skip_reason(what: str) -> str:
    """skipif 的 reason —— 必须写清"怎么开"，否则红灯/跳过都让人无从下手。"""
    return (
        f"{what}需要真实资金动作（人工扫码付款），当前闸门 = [{mode()}]。\n"
        "动态开启（任选其一，无需改代码）：\n"
        "  · 命令行：--real-pay=on（下新单并等扫码）/ --real-pay=auto（只用已付订单）\n"
        f"  · 环境变量：{ENV_VAR}=1 或 =auto\n"
        "  · 复用已付订单：MIGU_PAY_ORDER=<orderId>（真实支付续跑）、\n"
        "                  MIGU_REFUND_PARTIAL_ORDER=<orderId>（部分退指定单）\n"
        "  · run.sh：./run.sh all --real-pay=on"
    )


def label() -> str:
    """一行摘要，写进 Allure 报告附件里，避免"这轮到底花没花钱"说不清。"""
    text = {
        OFF: "off —— 跳过所有需要真实资金动作的用例（默认，最省）",
        AUTO: "auto —— 不花新钱，只用已付订单/门店现成订单",
        ON: "on —— 允许下新单并等真人扫码付款（会产生真实资金流水）",
    }[mode()]
    return f"真实支付闸门 [{ENV_VAR}/{mode()}]: {text}"
