# -*- coding: utf-8 -*-
"""下单前置校验（负向）—— 未开售场次不能下单。

为什么值得一条独立用例：
    「未到开售时间」是**上线当天最容易出事**的一类判断：
    开售时间配早了 → 活动提前放票；配晚了/丢了 → 到点买不了。
    正向往返（能正常下单）覆盖不了它，必须专门试"不该能下单的时候真的下单不了"。

夹具来源：
    清单里 event key = not_started 的场次：这些场次的票档在 C 端应带**未来的
    fixedSaleTime**（开售时间窗），下单必须被服务端以「未到开售时间」拒绝。

⚠️ 环境自适应（不会因为环境没克隆好而变红）：
    下单接口对**选座项目**会先做"选座商品请选择座位"的校验；
    「未开售」这场校验正好排在它**前面**，所以未开售项目不必真去选座。
    但如果该环境的票档压根没配开售时间窗（实测：生产 P10 未开售场次
    fixedSaleTime 全为 None），那它其实已经"可售"了 —— 此时本用例会
    **skip 并说明原因**（而不是失败），配置漂移交给
    `./run.sh catalog drift` 报（它会提示"未开售时间窗缺失"）。
"""
import json
import time

import allure
import pytest

from conftest import login_or_skip
from migu_api import catalog
from migu_api.client import MiguClient, MiguApiError

pytestmark = [pytest.mark.chain, pytest.mark.release_core]

# 目标：清单里 not_started 场次的 0 元票档（0 元 → 万一真下成了也不花钱）
try:
    _cands = [t for t in catalog.find(event="not_started") if float(t.price or 0) == 0]
except Exception:  # noqa: BLE001 —— 清单没加载好不该让收集期炸掉
    _cands = []
TARGET = _cands[0] if _cands else None
no_target = ("「测试专用商品清单」(catalog.json) 里没有 event=not_started 的 0 元票档，"
             "没法验证「未开售不可下单」")


def _future_sale_windows(product_id: int) -> list:
    """读 C 端票档列表（不需要登录态），返回**未来**的开售时间戳。"""
    skus = MiguClient().get_sku_list(product_id)
    now_ms = int(time.time() * 1000)
    out = []
    for s in skus:
        try:
            add = json.loads(s.get("addition") or "{}")
        except (TypeError, ValueError):
            continue
        ts = add.get("fixedSaleTime")
        if isinstance(ts, (int, float)) and ts > now_ms:
            out.append((int(s.get("skuId")), ts))
    return out


@pytest.fixture(scope="module")
def c_client():
    c = MiguClient()
    login_or_skip(c, context="下单前置校验 C 端登录态")
    return c


@allure.feature("下单前置校验")
@allure.story("未开售场次")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("未开售场次：到点前下单必须被拒（未到开售时间）")
@pytest.mark.skipif(TARGET is None, reason=no_target)
def test_order_rejected_before_sale(c_client):
    allure.attach(TARGET.label, name="夹具目标")

    with allure.step("① 确认该环境的未开售场次真的配了开售时间窗"):
        windows = _future_sale_windows(TARGET.product_id)
        allure.attach(
            "\n".join(f"skuId={sid} 开售时间={ts}" for sid, ts in windows) or "（无）",
            name="未来开售时间窗")
    if not windows:
        pytest.skip(
            f"环境 [{TARGET.env}] 的未开售场次没有配置未来的 fixedSaleTime"
            "（票档实际处于可售状态）→ 无法验证「未到开售时间」这条拦截。"
            "这是清单与环境的一处漂移，用 ./run.sh catalog drift 可看到"
            "「未开售时间窗缺失」；要真的覆盖请在 B 端给该场次票档配一个未来的开售时间。"
        )

    with allure.step(f"② 下单必须被拒：{TARGET.price} 元 / 1 张"):
        err = None
        try:
            order_id = c_client.create_order(TARGET.product_id, TARGET.sku_id, 1,
                                             TARGET.price, source="test_order_guards")
        except MiguApiError as exc:
            err = exc
            allure.attach(str(exc)[:400], name="拒绝响应",
                          attachment_type=allure.attachment_type.TEXT)
        else:
            allure.attach(f"orderId={order_id}", name="★ 意外下单成功")
        assert err is not None, (
            "未开售场次竟能下单成功 —— 开售时间校验失效（会提前放票）"
        )

    with allure.step("③ 拒绝原因必须是「开售时间」而不是别的（如选座缺失）"):
        msg = str(err)
        assert "开售" in msg, (
            f"被拒了，但不是因为未开售，而是别的原因：{msg[:200]}\n"
            "（说明开售时间校验没有生效，这条被别的前置校验挡下了）"
        )
