# -*- coding: utf-8 -*-
"""多次核销链路 —— 一张票分多次核销（「一日票/多日通票」的典型能力）。

为什么单独一条用例：
    test_b_verify.py 覆盖的是主链路（核销一次 → 票已核销、订单已完成）。
    但业务上还有一类票是**买一次、用多次**（多日通票/多次入场），
    配置点是**场次级 maxVerifyCount**（不是票档级）：
        多次核销场次 maxVerifyCount=3 → 同一张票可以核销 3 次，
        前 2 次核销后票仍可用、订单仍是待核销，第 3 次才置已核销 + 订单已完成。

夹具来源（不写死 ID）：
    catalog.json 里 key=multi_verify 的场次 × 该场次的 0 元票档
    （0 元 → 下单即出票，不花钱；不需要真实支付）。

环境差异（★ 重要）：
    测试环境已发布「核销次数」配置（P01 多次核销场次 = 3 次）；
    **生产未发布** —— B 端 events/edit 没有 maxVerifyCount 字段、
    票据 maxVerifyCount 恒为 1。所以在生产这条用例会自动跳过（而不是失败），
    跳过原因会写明"环境未发布该配置"。能力探测走清单里的 maxVerifyCount，
    catalog drift --deep 也会盯着这个值别漂。
"""
import json
import os
import time

import allure
import pytest

from conftest import login_or_skip
from migu_api import catalog
from migu_api.badmin import BAdminClient, BAdminAuthError, B_COOKIE_FILE
from migu_api.client import (MiguClient, STATUS_FINISHED, STATUS_PAID,
                             status_text)
from migu_api.config import prod_write_blocked, prod_write_skip_reason

pytestmark = [
    pytest.mark.b_verify,   # 跟主核销链路同一批：都要 B 端会话、都会真核销
    pytest.mark.release_core,
    pytest.mark.skipif(prod_write_blocked(), reason=prod_write_skip_reason()),
]

SOURCE = "test_multi_verify"

HAS_B_SESSION = B_COOKIE_FILE.exists() or bool(os.environ.get("MIGU_B_TICKET"))
skip_b = ("需要 B 端 4A 票据：./run.sh ticket renew（或 MIGU_B_TICKET='<iamcaspticket>'）")

# 目标：多次核销场次下的 0 元电子票票档，从清单按业务形态稳定选择。
# 不能只看 event=multi_verify：生产清单里保留了同名对照场次，但环境实际
# maxVerifyCount=1，不能拿来跑"多次核销"。
try:
    _hits = [
        t for t in catalog.find(
            event="multi_verify", sku="free", delivery="eticket", min_verify=2
        )
        if t.is_free
    ]
    _hits = sorted(_hits, key=lambda t: ("baseline" not in t.usages, t.project_key, t.sku_key))
    TARGET = _hits[0] if _hits else None
except catalog.CatalogLookupError as exc:  # 清单里没有这个项目/场次/票档
    TARGET, TARGET_ERR = None, str(exc)
except catalog.CatalogError as exc:
    TARGET, TARGET_ERR = None, str(exc)
else:
    TARGET_ERR = (
        None if TARGET else
        "清单里没有 event=multi_verify、maxVerifyCount>=2 且 sku=free 的 0 元电子票目标"
    )

EXPECT_N = TARGET.max_verify_count if TARGET else None
if TARGET and (EXPECT_N is None or EXPECT_N <= 1):
    no_feature = (
        f"环境 [{TARGET.env}] 未发布「核销次数」配置（该场次 maxVerifyCount={EXPECT_N}，"
        "生产 B 端 events/edit 无 maxVerifyCount 字段）→ 多次核销无法验证。"
        "测试环境该场次为 3 次，此用例在测试环境才会真正执行。"
    )
else:
    no_feature = None


@pytest.fixture(scope="module")
def c_client():
    c = MiguClient()
    login_or_skip(c, context="多次核销 C 端登录态")
    return c


@pytest.fixture(scope="module")
def b_client():
    b = BAdminClient()
    if os.environ.get("MIGU_B_TICKET"):
        try:
            b.login()
        except BAdminAuthError as e:
            pytest.skip(f"B 端票据兑换失败：{e}")
    return b


def _make_order(c_client) -> tuple[int, str]:
    """0 元下单并等出票（出票是异步的，别把"还没出票"当失败）。"""
    order_id = c_client.create_order(TARGET.product_id, TARGET.sku_id, 1,
                                     TARGET.price, source=SOURCE)
    deadline = time.time() + 30
    while time.time() < deadline:
        ticket = c_client.get_eticket(order_id)
        items = ticket if isinstance(ticket, list) else [ticket]
        items = [t for t in items if t and t.get("code")]
        if items:
            return order_id, items[0]["code"]
        time.sleep(2)
    raise AssertionError(f"订单 {order_id} 下单后 30s 仍未出票")


@allure.feature("多次核销")
@allure.story("一张票多次核销")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("多次核销：同一张票可核销 maxVerifyCount 次，用尽后才置已完成")
@pytest.mark.skipif(not HAS_B_SESSION, reason=skip_b)
@pytest.mark.skipif(TARGET is None, reason=TARGET_ERR or "清单里没有多次核销目标")
@pytest.mark.skipif(no_feature is not None, reason=no_feature or "")
def test_multi_verify_chain(c_client, b_client):
    n = int(EXPECT_N)
    allure.attach(TARGET.label, name="夹具目标")

    with allure.step(f"① C 端 0 元下单并出票（场次核销上限 {n} 次）"):
        order_id, code = _make_order(c_client)
        allure.attach(f"orderId={order_id} 券码={code}", name="C端夹具")

    with allure.step("② 等 B 端可见，读初始核销次数"):
        assert b_client.wait_order(order_id, timeout=40), f"B 端查不到订单 {order_id}"
        before = b_client.get_eticket(order_id)
        allure.attach(json.dumps(before, ensure_ascii=False, indent=1)[:600],
                      name="核销前票据")
        assert before.get("totalVerifyCount") == n, (
            f"工单期望本场次核销上限 {n} 次，票据却给 {before.get('totalVerifyCount')} —— "
            "清单登记的 maxVerifyCount 与环境实况不一致（跑 ./run.sh catalog drift --deep 看）"
        )
        assert before.get("usedVerifyCount") == 0, "新票已核销次数应为 0"
        assert before.get("status") == 1, f"新票状态应为未核销(1)，实际 {before.get('status')}"

    with allure.step(f"③ 连核 {n} 次：前 {n - 1} 次票/订单都应保持可用"):
        for i in range(1, n + 1):
            rc, msg = b_client.verify_result(code)
            allure.attach(f"第 {i} 次核销 → code={rc} message={msg}",
                          name=f"核销第{i}次")
            assert rc == 200, f"第 {i}/{n} 次核销应成功，实际 code={rc} {msg}"

            time.sleep(1)
            t = b_client.get_eticket(order_id)
            with allure.step(f"第 {i} 次核销后：已核销 {t.get('usedVerifyCount')}/{n}"):
                allure.attach(json.dumps(t, ensure_ascii=False, indent=1)[:600],
                              name=f"核销后票据(第{i}次)")
            assert t.get("usedVerifyCount") == i, (
                f"第 {i} 次核销后 usedVerifyCount 应为 {i}，实际 {t.get('usedVerifyCount')}")
            assert t.get("remainVerifyCount") == n - i, (
                f"第 {i} 次核销后剩余次数应为 {n - i}，实际 {t.get('remainVerifyCount')}")
            # 逐次留痕在票据的 verifyRecordList 里（verifySeq 1..N、操作人、时间）
            seqs = [r.get("verifySeq") for r in (t.get("verifyRecordList") or [])]
            assert seqs == list(range(1, i + 1)), (
                f"第 {i} 次核销后 verifyRecordList 的 verifySeq 应为 1..{i}，实际 {seqs}")

            order = b_client.get_order(order_id)
            if i < n:
                # 还没用尽：票仍可用、订单仍是待核销 —— 这是"多次核销"最关键的一条
                assert t.get("status") == 1, (
                    f"第 {i}/{n} 次核销后票应为未核销(1)（还有 {n - i} 次可核），"
                    f"实际 {t.get('status')} —— 一次性把票置成已核销就等于丢了剩余次数")
                assert order and order.get("status") == STATUS_PAID, (
                    f"第 {i}/{n} 次核销后订单应仍为 {status_text(STATUS_PAID)}，"
                    f"实际 {order and order.get('statusName')}")
            else:
                # 用尽：票已核销、订单已完成
                assert t.get("status") == 2, (
                    f"第 {n} 次（最后）核销后票应为已核销(2)，实际 {t.get('status')}")

    with allure.step(f"④ 用尽后订单应变已完成({STATUS_FINISHED})"):
        deadline = time.time() + 20
        order = None
        while time.time() < deadline:
            order = b_client.get_order(order_id)
            if order and order.get("status") == STATUS_FINISHED:
                break
            time.sleep(2)
        allure.attach(json.dumps(
            {k: order.get(k) for k in ("status", "statusName", "statusText")},
            ensure_ascii=False), name="最终订单")
        assert order and order.get("status") == STATUS_FINISHED, (
            f"核销次数用尽后订单应为已完成({STATUS_FINISHED})，"
            f"实际 {order and order.get('statusName')}")

    with allure.step("⑤ 第 N+1 次核销必须被拒（上限校验）"):
        rc, msg = b_client.verify_result(code)
        allure.attach(f"code={rc} message={msg}", name="超限核销响应")
        assert rc != 200, "次数已用尽，重复核销居然成功了 —— 上限校验失效"
        assert "核销" in (msg or ""), f"拒绝原因不明确：{msg}"

    with allure.step("⑥ 核销记录可查（留痕）"):
        records = b_client.verify_records(code)
        allure.attach(json.dumps(records, ensure_ascii=False, indent=1)[:800],
                      name="核销记录")
        # ⚠️ 口径：/v1/verification/voucher 是**按券码聚合**的一条（含最后核销时间与
        # 最终状态），逐次流水在票据的 verifyRecordList 里（第③步已逐次断过 verifySeq）。
        # 所以这里不能断言 == n 条，否则会把"正常聚合"误报成"留痕不全"。
        assert len(records) == 1, (
            f"券码查询接口按券码聚合，应恰好 1 条，实际 {len(records)} 条")
        assert records[0].get("code") == code, "记录里的券码与本次核销的不是同一张"
        allure.attach(
            f"该接口按券码聚合 {len(records)} 条（最终状态={records[0].get('status')}，"
            f"最后核销时间={records[0].get('verificationTime')}）；"
            f"逐次流水共 {n} 条已由票据 verifyRecordList 覆盖。",
            name="留痕口径", attachment_type=allure.attachment_type.TEXT,
        )
