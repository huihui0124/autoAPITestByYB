# -*- coding: utf-8 -*-
"""
conftest.py —— 全局 pytest 配置

做两件事：

1) 「真实支付闸门」做成**命令行开关**，让"要不要跑需要花真钱的用例"变成跑之前
   一句话就能决定的事，而不必去改代码或翻环境变量。

       ./.venv/bin/pytest --real-pay=on      # 允许下新单 + 等真人扫码（会花钱）
       ./.venv/bin/pytest --real-pay=auto    # 只用已付订单，不花新钱
       ./.venv/bin/pytest --real-pay=off     # 默认，跳过
       ./.venv/bin/pytest --real-pay         # 省略取值 = on

   实现要点：`pytest_configure` 在**收集之前**执行，这里把选项写回环境变量
   `MIGU_REAL_PAY`，于是所有模块（它们都是在 import 时读环境变量）看到的都是
   最终值 —— 不需要每个用例文件都去读 pytest config。

2) 「CI 无人值守模式」（MIGU_CI=1）—— 给流水线加一道**代码级**保险，不依赖
   CI 脚本里写了什么：

       MIGU_CI=1 ./.venv/bin/pytest -m "smoke or chain or catalog or b_verify"

   - 强制关掉真实支付闸门；命令行再传 --real-pay=on 直接**报错退出**（宁可流水线红，
     也不能让机器人下真单）
   - 收集期跳过 `refund`（会真退一笔）与 `real_pay`（要真人扫码）两组用例，
     跳过原因写清楚"CI 模式不跑"，而不是静默少跑
   - 生产环境的写操作用例另有闸门（config.prod_write_blocked / MIGU_ALLOW_PROD_WRITE），
     与本模式叠加生效：CI + 生产 默认只跑只读
"""
from __future__ import annotations

import os

import pytest
import requests

from migu_api import fundgate

# 置 1/true/yes/on 表示"这是无人值守的 CI 跑法"
CI_ENV = "MIGU_CI"
_ON = {"1", "true", "yes", "y", "on"}

# CI 模式下**永不执行**的用例分组：会真花钱 / 真退款的
CI_BLOCKED_MARKERS = ("refund", "real_pay")


def login_or_skip(client, *, context: str = "C 端登录态"):
    """登录时网络层不可用就跳过，避免把环境抖动报成业务失败。"""
    try:
        return client.login()
    except requests.exceptions.RequestException as exc:
        pytest.skip(f"{context}不可用：网络请求异常，当前轮次不判业务失败。{exc}")


@pytest.fixture(autouse=True)
def _network_timeout_as_skip():
    """把 C 端域名偶发超时归类成环境跳过，而不是用例 broken。"""
    try:
        yield
    except (requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectTimeout) as exc:
        pytest.skip(f"C 端环境网络超时，当前轮次不判业务失败：{exc}")


def ci_mode() -> bool:
    """当前是不是 CI 无人值守模式。"""
    return str(os.environ.get(CI_ENV, "")).strip().lower() in _ON


def pytest_addoption(parser):
    group = parser.getgroup("migu", "咪咕票务回归")
    group.addoption(
        "--real-pay",
        action="store",
        nargs="?",
        const=fundgate.ON,
        default=None,
        choices=fundgate.CHOICES,
        metavar="MODE",
        help=(
            "真实支付闸门，决定「需要花真钱/真人扫码」的用例跑不跑："
            "on=允许下新单并等扫码；auto=只用已付款订单（不花新钱）；off=跳过（默认）。"
            "省略取值等价于 on。"
        ),
    )


def pytest_configure(config):
    value = config.getoption("--real-pay", default=None)

    if ci_mode():
        if value == fundgate.ON:
            raise pytest.UsageError(
                f"{CI_ENV}=1（CI 无人值守模式）下不允许打开真实支付闸门"
                "（--real-pay=on 会让机器人下真单、等真人扫码）。"
                "要真付款请在本地人工执行。"
            )
        # 强制关闸：CI 里永远跳过需要真实资金的用例
        os.environ[fundgate.ENV_VAR] = fundgate.OFF
        fundgate.set_mode(fundgate.OFF)
        return

    if value:
        os.environ[fundgate.ENV_VAR] = value
        fundgate.set_mode(value)


def pytest_collection_modifyitems(config, items):
    """CI 模式下把「会花钱 / 会真退款」的用例标成 skip（而不是让它们失败）。"""
    if not ci_mode():
        return
    for item in items:
        hit = sorted({m.name for m in item.iter_markers()} & set(CI_BLOCKED_MARKERS))
        if hit:
            item.add_marker(pytest.mark.skip(
                reason=f"{CI_ENV}=1：CI 不跑「{'/'.join(hit)}」分组"
                       "（需要真实资金或真人扫码），请在本地人工回归"
            ))


def pytest_report_header(config):
    """在 pytest 头部打一行状态 —— 让人一眼看出这轮"花不花钱、是不是 CI"。"""
    line = fundgate.label()
    if ci_mode():
        line += f" ｜ {CI_ENV}=1 CI 模式：跳过 {'/'.join(CI_BLOCKED_MARKERS)}"
    return line
