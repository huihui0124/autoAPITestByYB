# -*- coding: utf-8 -*-
"""
test_real_pay.py —— 真实支付闭环用例（人工付款 0.01 元）

覆盖链路：下单 → 收银台 → 获取支付参数 → **人工真实付款** → 订单变已支付 → 电子票出票

为什么要单独一个文件：
    test_core_chain.py 是无人值守回归（只到"网关受理"这一层）；
    这条用例必须真人扫码/付款，所以默认 **跳过**，不会被 CI/定时任务误跑。

运行方式（手动；闸门见 migu_api/fundgate.py）：
    ./.venv/bin/pytest test_real_pay.py -s --real-pay=on          # 测试环境，默认微信
    ./.venv/bin/pytest test_real_pay.py -s --real-pay=on MIGU_PAY_CHANNEL=AP
    MIGU_REAL_PAY=1 MIGU_PAY_TIMEOUT=300 ./.venv/bin/pytest test_real_pay.py -s

    # 生产环境（真钱 0.01 元，走生产测试店 storeId=888，只有支付宝）：
    MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 \
        ./.venv/bin/pytest test_real_pay.py -s --real-pay=on MIGU_PAY_TIMEOUT=900

    -s 是必须的：二维码路径会打在终端，且要留时间给你扫码。
    只出码不等结果（自己盯着付）也可以用 ./.venv/bin/python pay_scan.py --prepare。
    run 之前建议先跑 `.venv/bin/python pay_scan.py` 确认支付通道正常。

续跑（付款成功后后续步骤失败时，别重复花钱；不需要 --real-pay）：
    MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 MIGU_PAY_ORDER=61008637 \
        ./.venv/bin/pytest test_real_pay.py -s
    → 跳过下单/出码/等待，直接从"订单已支付"往下跑出票与核销。

关键结论（2026-09-29 实测跑通）：
    - 能闭环的是 /v1/miguPay/pay（创建支付记录），不是 /v1/miguPay/test/pay
      （后者文档写明"不涉及业务逻辑，仅验证协议对接"，付款不会让订单变已支付）
    - 支付宝返回 HTML 表单（需浏览器/扫码打开），微信返回 https 链接（直接出二维码）
    - 已支付订单状态是 11（待使用），不是 2/3/5
    - 支付成功后 /v1/order/{id}/eticket 立即出票，带核销码 + maxVerifyCount（多次核销用）
"""
import json
import os
import time
from pathlib import Path

import allure
import pytest

from migu_api import catalog, fundgate, paypage, qr
from migu_api.badmin import BAdminClient, B_COOKIE_FILE
from migu_api.fsutil import ensure_dir
from migu_api.client import (MiguClient, PAID_STATUS, STATUS_FINISHED,
                             STATUS_PAID, STATUS_UNPAID, status_text)
from migu_api.config import env, prod_write_blocked, prod_write_skip_reason

# 分组标记：真实支付（需人工付款）
# 跑不跑由「真实支付闸门」动态决定，不再写死跳过 —— 见 migu_api/fundgate.py：
#   --real-pay=on（下新单 + 等扫码）/ auto（只用已付订单）/ off（默认跳过）
pytestmark = [
    pytest.mark.real_pay,
    pytest.mark.release_money,
    # 生产写操作闸门：真实支付会下单 + 核销，生产默认拦掉
    pytest.mark.skipif(prod_write_blocked(), reason=prod_write_skip_reason()),
]

# 下单目标**从「测试专用商品清单」按用途取**（usage=paid），不再写死商品 ID。
# 清单里没有 → 本文件自动跳过（而不是拿测试环境的 ID 去生产乱下单）。
try:
    PAY_TARGET = catalog.one(usage="paid")
    PRODUCT_ID, SKU_ID, TOTAL_AMOUNT = (
        PAY_TARGET.product_id, PAY_TARGET.sku_id, PAY_TARGET.price)
except catalog.CatalogError:
    PAY_TARGET = None
    PRODUCT_ID, SKU_ID, TOTAL_AMOUNT = (None, None, "0.01")

SOURCE = "test_real_pay"
ARTIFACTS = Path(__file__).parent / "artifacts"
# 产物文件名带环境前缀：测试/生产的付款证据要能并排存放、互不覆盖
ENV_TAG = env.name

REAL_PAY = fundgate.allows_new_payment()      # 闸门 = on：允许下新单并等扫码
FUND_MODE = fundgate.mode()
# 默认渠道按环境取值（测试 WX / 生产 AP），仍可用环境变量临时覆盖
CHANNEL = os.environ.get("MIGU_PAY_CHANNEL", env.pay_bank_code).upper()
PAY_TIMEOUT = fundgate.pay_timeout()
# 续跑模式：支付已经成功、但后续步骤失败时，用 MIGU_PAY_ORDER=<orderId> 复用这笔已付订单，
# 跳过下单/出码/等待，直接从"订单已支付"往下跑出票与核销。
# 为什么必需：真实支付是"钱花了就没法重来"的动作，后面任何一步断言写错（本用例就踩过
# verifiedCount 那条）都会逼着人再付一次钱。有了续跑，失败后补跑不重复花钱。
# 指定了 MIGU_PAY_ORDER 也意味着"闸门至少 auto"（见 fundgate.mode()）。
RESUME_ORDER = os.environ.get("MIGU_PAY_ORDER") or None
# 有 B 端会话就顺带把"多次核销 → 订单已完成"也跑完，没有就跳过那一段
HAS_B_SESSION = B_COOKIE_FILE.exists() or bool(os.environ.get("MIGU_B_TICKET"))

skip_reason = fundgate.skip_reason("真实支付闭环（下单→真人付款→出票→核销）")
no_pay_product = ("「测试专用商品清单」(catalog.json) 里没有 usage=paid 的可下单目标 —— "
                  "先在 B 端建好 0.01 元电子票商品，再用 ./run.sh catalog add 登记")


@pytest.fixture(scope="module")
def client():
    c = MiguClient()
    c.login()
    return c


def _wait_order_status(client, order_id, want: int, timeout: int = 30) -> dict:
    """轮询订单详情直到 status==want（或超时），返回最后一次详情。"""
    deadline = time.time() + timeout
    detail = client.get_order_detail(order_id)
    while time.time() < deadline and detail.get("status") != want:
        time.sleep(2)
        detail = client.get_order_detail(order_id)
    return detail


def _verify_multi(b, client, order_id, code, max_verify: int) -> None:
    """多次核销路径（测试环境已发布该特性）：逐次核销，次数用尽后订单变已完成。"""
    assert b.wait_order(order_id, timeout=40), "B 端查不到该订单"
    for seq in range(1, max_verify + 1):
        with allure.step(f"第 {seq} 次核销"):
            b.verify(code)
            time.sleep(1)
            snap = b.get_eticket(order_id)
            allure.attach(json.dumps(
                {k: snap.get(k) for k in ("status", "totalVerifyCount",
                                          "usedVerifyCount", "remainVerifyCount")},
                ensure_ascii=False), name=f"第{seq}次后")
            assert snap.get("usedVerifyCount") == seq, f"第{seq}次核销次数不符"
    with allure.step("重复核销应被拒绝"):
        rc, msg = b.verify_result(code)
        allure.attach(f"code={rc} message={msg}", name="超限核销")
        assert rc != 200, "超过次数上限仍核销成功"
    with allure.step("校验订单变已完成(7)"):
        detail = _wait_order_status(client, order_id, STATUS_FINISHED, timeout=30)
        allure.attach(status_text(detail.get("status")), name="核销后订单状态")
        assert detail.get("status") == STATUS_FINISHED, (
            f"次数用尽后订单应已完成({STATUS_FINISHED})，"
            f"实际 {status_text(detail.get('status'))}"
        )


def _verify_single(b, client, order_id, code) -> None:
    """单次核销路径（生产：多次核销特性未发布，活动 verificationNum 全为 1）。

    生产票据接口**不返回** maxVerifyCount / usedVerifyCount（实测 2026-09-29），
    所以断言口径降级为三处等价证据，与 test_b_verify.py 的降级模式保持一致：
        ① B 端核销响应 = 核销成功
        ② 票状态 1(未核销) → 2(已核销)
        ③ 订单 11(待使用) → 7(已完成)
    外加反例：重复核销必须被拒（证明上限校验生效，不是"随便核销都成功"）。
    """
    with allure.step("等 B 端检索索引收录该订单（实测 3~10s）"):
        assert b.wait_order(order_id, timeout=60), (
            f"B 端查不到订单 {order_id}（跨系统数据不一致或索引延迟）")
        before = b.get_eticket(order_id)
        allure.attach(json.dumps(before, ensure_ascii=False, indent=1)[:800],
                      name="核销前票据")
    assert before.get("status") == 1, (
        f"新票状态应为未核销(1)，实际 {before.get('status')}")

    with allure.step(f"B 端核销券码 {code}"):
        t0 = time.time()
        result = b.verify(code)
        allure.attach(f"{json.dumps(result, ensure_ascii=False)} "
                      f"(耗时 {round(time.time() - t0, 1)}s)", name="核销响应")

    with allure.step("校验票状态 1 → 2（已核销）"):
        time.sleep(1)
        after = b.get_eticket(order_id)
        allure.attach(json.dumps(after, ensure_ascii=False, indent=1)[:800],
                      name="核销后票据")
        assert after.get("status") == 2, (
            f"核销后票状态应为已核销(2)，实际 {after.get('status')}")

    with allure.step("重复核销应被拒绝（上限校验）"):
        rc, msg = b.verify_result(code)
        allure.attach(f"code={rc} message={msg}", name="重复核销响应")
        assert rc != 200, "重复核销居然成功了，上限校验失效"

    with allure.step("校验订单变已完成(7)"):
        detail = _wait_order_status(client, order_id, STATUS_FINISHED, timeout=30)
        allure.attach(status_text(detail.get("status")), name="核销后订单状态")
        assert detail.get("status") == STATUS_FINISHED, (
            f"单次核销票用完后订单应已完成({STATUS_FINISHED})，"
            f"实际 {status_text(detail.get('status'))}"
        )


@allure.feature("核心链路(真实支付)")
@allure.story("支付闭环")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("真实支付：下单→人工付款→订单变已支付→电子票出票")
@pytest.mark.skipif(not (REAL_PAY or RESUME_ORDER), reason=skip_reason)
@pytest.mark.skipif(PAY_TARGET is None, reason=no_pay_product)
def test_real_pay_closed_loop(client):
    """全链路：只有"订单真的变成已支付 + 电子票出得来"才算通过。

    两种跑法：
      正常：下单 → 出码 → 人工付款 → 出票 → （可选）B 端核销
      续跑：MIGU_PAY_ORDER=<orderId> 复用一笔已付订单，跳过下单/出码/等待，
            从"订单已支付"接着走 —— 用于付款成功后后续步骤失败时补跑，不重复花钱
    """
    pay_url = png = None
    allure.attach(fundgate.label(), name="本轮真实支付闸门")

    if RESUME_ORDER:
        # ---------- 续跑：复用已支付订单 ----------
        order_id = int(RESUME_ORDER)
        with allure.step(f"续跑模式：复用已支付订单 {order_id}（不重新下单、不重新付款）"):
            detail = client.get_order_detail(order_id)
            allure.attach(str(order_id), name="orderId")
            allure.attach(status_text(detail.get("status")), name="订单当前状态")
            allure.attach(
                "由 MIGU_PAY_ORDER 指定。场景：真实支付已成功，但后续步骤失败 —— "
                "由于钱已经花掉没法重来，用续跑把出票/核销补齐，避免再付一次。\n"
                "本轮的付款过程证据见上一轮（付款前）报告的『等待支付结果』步骤。",
                name="续跑说明", attachment_type=allure.attachment_type.TEXT)
        assert detail.get("status") in PAID_STATUS, (
            f"续跑要求该订单已支付，实际 {status_text(detail.get('status'))}；"
            "未支付的订单请正常跑（不带 MIGU_PAY_ORDER）"
        )
        allure.dynamic.title(
            f"真实支付（续跑）：订单 {order_id} 已支付 → 电子票 → B端核销 → 已完成")
    else:
        # ---------- 1. 下单 ----------
        with allure.step(f"创建订单：商品 {PRODUCT_ID} / SKU {SKU_ID} / {TOTAL_AMOUNT} 元"):
            order_id = client.create_order(PRODUCT_ID, SKU_ID, 1, TOTAL_AMOUNT,
                                           source=SOURCE)
            detail = client.get_order_detail(order_id)
            allure.attach(str(order_id), name="orderId")
            allure.attach(status_text(detail.get("status")), name="初始状态")
        assert detail.get("status") == STATUS_UNPAID, (
            f"下单后应为待付款({STATUS_UNPAID})，实际 {status_text(detail.get('status'))}"
        )

        # ---------- 2. 收银台 ----------
        with allure.step("获取收银台详情（金额/渠道/倒计时）"):
            pay_detail = client.get_pay_detail(order_id)
            allure.attach(json.dumps(pay_detail, ensure_ascii=False, indent=1),
                          name="payDetail", attachment_type=allure.attachment_type.JSON)
        assert float(pay_detail.get("payAmount") or 0) == float(TOTAL_AMOUNT), "收银台金额不符"
        assert int(pay_detail.get("leftTime") or 0) > 0, "支付倒计时异常（订单可能已过期）"

        # ---------- 3. 创建支付 + 出可付的东西 ----------
        with allure.step(f"创建支付记录（渠道 {CHANNEL}）并生成付款入口"):
            pay = client.create_payment(order_id, bank_code=CHANNEL,
                                        pay_amount=TOTAL_AMOUNT)
            params = pay.get("paymentParams") or ""
            allure.attach(paypage.describe(params), name="支付参数形态")
        assert params, f"未返回支付参数：{pay}"

        if paypage.is_link(params):
            pay_url = paypage.strip_cdata(params)
            png = qr.save_png(pay_url, ARTIFACTS / f"{ENV_TAG}_pay_qr_{order_id}.png")
        else:
            html = paypage.save_form_html(
                params, ARTIFACTS / f"{ENV_TAG}_pay_form_{order_id}.html")
            pay_url = paypage.to_pay_url(params)
            png = qr.save_png(pay_url, ARTIFACTS / f"{ENV_TAG}_pay_qr_{order_id}.png")
            allure.attach.file(str(html), name="支付宝收银台表单页",
                               attachment_type=allure.attachment_type.HTML)

        with allure.step("二维码已生成，请扫码付款"):
            allure.attach.file(str(png), name="支付二维码",
                               attachment_type=allure.attachment_type.PNG)
            print(f"\n{'=' * 64}\n请扫码支付 {TOTAL_AMOUNT} 元"
                  f"（环境 {env.name} / 店铺 {env.store_id} / 渠道 {CHANNEL}）\n"
                  f"二维码：{png}\n链接：{pay_url}\n{'=' * 64}\n"
                  f"订单倒计时 {int(pay_detail.get('leftTime') or 0) // 1000}s，请尽快付款\n",
                  flush=True)

        # ---------- 4. 等人工付款 ----------
        with allure.step(f"等待支付结果（最长 {PAY_TIMEOUT}s）"):
            t0 = time.time()
            paid = client.wait_for_paid(order_id, timeout=PAY_TIMEOUT, interval=3,
                                        verbose=True)
            cost = int(time.time() - t0)
            detail = client.get_order_detail(order_id)
            allure.attach(f"status={status_text(detail.get('status'))} 耗时={cost}s",
                          name="支付结果")
            print(f"\n付款结果：paid={paid} status={status_text(detail.get('status'))} "
                  f"耗时={cost}s")
        assert paid, (f"等待 {PAY_TIMEOUT}s 仍未支付成功，"
                      f"订单状态 {status_text(detail.get('status'))}")

    # ---------- 5. 断言订单状态 ----------
    with allure.step("校验订单状态为已支付(11=待使用)"):
        assert detail.get("status") in PAID_STATUS, (
            f"期望已支付{PAID_STATUS}，实际 {status_text(detail.get('status'))}"
        )
        assert detail.get("status") == STATUS_PAID, "状态与实测值 11 不一致（枚举可能变了）"
        assert float(detail.get("paymentPrice") or 0) == float(TOTAL_AMOUNT), "支付金额不符"

    # ---------- 6. 电子票 ----------
    with allure.step("校验电子票已出票（含核销码）"):
        eticket = client.get_eticket(order_id)
        allure.attach(json.dumps(eticket, ensure_ascii=False, indent=1),
                      name="eticket", attachment_type=allure.attachment_type.JSON)
        print(f"电子票：{json.dumps(eticket, ensure_ascii=False)[:300]}")

    assert eticket, "支付成功但未出票"
    ticket = eticket[0] if isinstance(eticket, list) else eticket
    assert ticket.get("code"), "电子票没有核销码"
    assert ticket.get("canConsume") is True, "电子票不可核销"
    # 「多次核销」未发布的环境（生产）票据接口**不返回** verifiedCount/maxVerifyCount，
    # 所以次数断言必须按字段存在与否降级，否则读出来是 None 会误判成"新票核销过"
    # （踩过：2026-09-29 生产真实支付用例就是挂在这一行）
    if "verifiedCount" in ticket:
        assert ticket.get("verifiedCount") == 0, "新票核销次数应为 0"
    else:
        allure.attach(
            f"环境 [{env.name}] 票据接口不返回 verifiedCount（未发布多次核销），"
            f"改用票状态判断。票状态={ticket.get('status')}",
            name="核销次数（降级模式）", attachment_type=allure.attachment_type.TEXT)
        assert ticket.get("status") == 1, (
            f"新票状态应为未核销(1)，实际 {ticket.get('status')}")
    assert ticket.get("expired") is False, "新票不应过期"

    # ---------- 7. （可选）B 端核销，跑到订单"已完成" ----------
    # 核销是 B 端的动作，需要 B 端登录态；没有就跳过这一段（不影响支付用例结论）。
    # 断言口径按环境能力分叉：
    #   测试环境（已发布多次核销）→ 按 maxVerifyCount 逐次核销 + 次数断言
    #   生产环境（未发布）        → 票据无次数字段，降级为单次核销 + 状态断言
    with allure.step("B端核销（需 B 端会话）"):
        if not HAS_B_SESSION:
            allure.attach("无 B 端会话，跳过核销段（先跑 ./run.sh ticket ensure 再重跑）",
                          name="跳过说明")
        else:
            b = BAdminClient()
            max_verify = ticket.get("maxVerifyCount")
            if max_verify:
                allure.attach(
                    f"环境 [{env.name}] 已发布多次核销，券码上限 {max_verify} 次",
                    name="核销模式")
                _verify_multi(b, client, order_id, ticket["code"], max_verify)
            else:
                allure.attach(
                    f"环境 [{env.name}] 未发布多次核销特性（票据接口不返回 "
                    "maxVerifyCount/usedVerifyCount）→ 走单次核销降级路径：\n"
                    "断言 B 端核销成功、票状态 1→2、订单 11→7、重复核销被拒",
                    name="核销模式（降级）", attachment_type=allure.attachment_type.TEXT)
                _verify_single(b, client, order_id, ticket["code"])

    # 核销跑完后订单多半已到终态（7 已完成），重取一次，
    # 保证落盘结果与终端输出反映的是**最终**状态而不是进入本段时的快照。
    # （踩过：续跑模式下 detail 是开头取的，核销完仍在用旧值，打印出 "status=11"）
    detail = client.get_order_detail(order_id)

    ensure_dir(ARTIFACTS)
    payload = json.dumps(
        {"env": env.name, "storeId": env.store_id, "orderId": order_id,
         "channel": CHANNEL, "paid": True, "orderStatus": detail.get("status"),
         "orderStatusText": status_text(detail.get("status")),
         "paymentPrice": detail.get("paymentPrice"),
         "payUrl": pay_url, "qrPng": str(png) if png else None,
         "resumed": bool(RESUME_ORDER), "eticket": eticket},
        ensure_ascii=False, indent=1)
    (ARTIFACTS / f"{ENV_TAG}_last_pay.json").write_text(payload, encoding="utf-8")
    # 兼容旧路径（历史脚本/文档按 last_pay.json 找结果）
    (ARTIFACTS / "last_pay.json").write_text(payload, encoding="utf-8")
    print(f"\n✓ 本轮真实支付闭环完成：env={env.name} orderId={order_id} "
          f"status={status_text(detail.get('status'))}"
          f"{'（续跑）' if RESUME_ORDER else ''} 结果已写入 "
          f"{ARTIFACTS / f'{ENV_TAG}_last_pay.json'}")
