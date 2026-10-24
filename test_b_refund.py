# -*- coding: utf-8 -*-
"""
test_b_refund.py —— B 端退款闭环：可退性 → 试算 → 提交 → 审批 → 已退款

为什么单独立一个文件：
    退款是 B 端的第二条业务链路（第一条是核销），而且**带审批**、**动真钱**，
    性质比核销更重。跟核销用例混在一起会让"哪条挂了"难以区分。

被测链路（全部实测走通，2026-09-29，测试 store 1 + 生产 store 888 双环境）：
    1. GET  /v1/shopTrade/refund/refundable/{unifiedOrderId}  可退性
    2. POST /v1/shopTrade/refund/calculate                    试算（不改数据）
    3. POST /v1/shopTrade/refund/partial                      提交 -> 返回整数 refundId
    4. POST /v1/shopTrade/refund/list                         回查工单 -> 拿 taskId
    5. POST /v1/shopTrade/refund/approval                     审批（approvalStatus=2 同意）
    6. GET  /v1/shopTrade/refund/detail/{taskId}              轮询到 taskStatus=2 成功
    终态：订单 status -> 5(已退款)，且该订单**不再可退**

部分退款（2026-09-29 实测双环境打通）：
    "部分退"= 一个订单项有 N 份，只退其中的 M 份（M < N），剩余份数仍可继续退。
    调用契约（**最容易踩的地方**）：
        `refundTicketCount` / `refundMoneyCount` 只决定"记几份"，
        **真正退多少钱由 `itemOriginalAmount` 决定** —— 两者必须一致，
        否则留下脏数据（实测：3 份的项传 份数=1 + 金额=3份的钱 →
        真退 3 份的钱，却只记 refundedCount=1、剩 remainCount=2 而 remain 金额已为 0）。
        所以 `itemOriginalAmount = unitPrice × 本次份数`（`build_items` 已按此实现）。
    判定部分退成功的硬指标：退款后 `refundedCount += M`、`remainCount -= M`、
        `remainRefundableAmount -= M×unitPrice`，且 **remainCount 仍 > 0（还能再退）**。
    ⚠️ 小额订单的**取整精度**：单价 1 分 × 3 份的单，退 1 份的试算会被按比例分摊
        抹成 0.00 元（退 2 份 = 0.02、退 3 份 = 0.03 都正常）。这不是部分退功能坏了，
        用例会自动改用一个"退得动钱"的份数，所以 MIGU_REFUND_PARTIAL_N 只是首选值。

跑之前要有什么：
    - B 端会话（`.migu_state/<env>/b_cookies.json`，失效就 `./run.sh ticket ensure`）
    - 一笔**可退**的订单。优先级：
        ① 环境变量 MIGU_REFUND_ORDER=<orderId> 显式指定（真实支付后最常用）
        ② 自动在店铺里找一笔可退订单（优先"有金额"的，其次 0 元夹具）
      ⚠️ 退款是**一次性**的：同一订单退完就不能再退，所以自动找单每次会换一笔。
         反复跑会持续消耗店铺里的可退订单，跑之前先确认还有货。
    - 部分退的用例额外需要一笔 **remainCount >= 2** 的订单（自动找单会挑；
      也可用 MIGU_REFUND_PARTIAL_ORDER 显式指定）。找不到时按**真实支付闸门**
      （migu_api/fundgate.py）决定怎么补，不再写死跳过：
        · 闸门 = on（`--real-pay=on` / MIGU_REAL_PAY=1）
          → 自动下一笔**付费**多份单、打印二维码、**阻塞等你扫码付款**，
            付完接着把部分退跑完。带资金维度（金额断言生效），会花真钱
            （测试 0.03 元起，生产 0.03 元）。生产的部分退 bug 只有这条路径能暴露。
        · 闸门 = off/auto
          → 测试环境：自造一笔 **0 元**多份夹具单（`env.supports_free_multi_fixture`），
            免费但只有份数/状态维度，报告里会标"0元夹具单(无资金维度)"；
            生产：跳过（**生产禁用 0 元夹具**，原因见 config.py —— 0 元单是假绿）。

用法：
    ./.venv/bin/pytest test_b_refund.py -v                       # 测试环境
    MIGU_ALLOW_PROD_WRITE=1 ./.venv/bin/pytest test_b_refund.py   # 生产（需闸门）
    MIGU_REFUND_ORDER=61008681 ./.venv/bin/pytest test_b_refund.py # 指定订单
    MIGU_REFUND_WHAT=money ./.venv/bin/pytest test_b_refund.py    # 只退钱不退票
    MIGU_REFUND_PARTIAL_N=2 ./.venv/bin/pytest test_b_refund.py   # 部分退每次退 2 份
"""
import json
import os
import time
from pathlib import Path

import allure
import pytest

from migu_api import catalog, fundgate, paypage, qr
from migu_api.badmin import BAdminError
from migu_api.client import ORDER_STATUS_TEXT, REFUNDED_STATUS, MiguClient
from migu_api.config import env, prod_write_blocked, prod_write_skip_reason
from migu_api.fsutil import ensure_dir
from migu_api.refund import (
    RefundClient,
    TASK_SUCCESS,
    WHAT_BOTH,
    WHAT_MONEY,
    WHAT_TICKET,
    task_status_desc,
    what_desc,
)

WHAT = os.environ.get("MIGU_REFUND_WHAT", WHAT_BOTH).lower()
WHAT_CHOICES = {WHAT_BOTH, WHAT_MONEY, WHAT_TICKET}
WANT_TICKET = 1 if WHAT in (WHAT_TICKET, WHAT_BOTH) else 0
WANT_MONEY = 1 if WHAT in (WHAT_MONEY, WHAT_BOTH) else 0
WHAT_LABEL = what_desc(WANT_TICKET, WANT_MONEY)

# 部分退：本次退几份（会在 fixture 里裁剪成 < remainCount，保证"退不满"）
PARTIAL_N = max(1, int(os.environ.get("MIGU_REFUND_PARTIAL_N", "1")))

# 退款成功后**订单**的状态随商品/履约类型而变（实测 2026-09-29）：
#   电子票类商品 → 5(已退款)          —— 票作废 + 钱退回，一步到位
#   实物/自取类商品 → 14(售后中)      —— 退款已受理（工单照样成功、钱照样退），
#                                       但订单进入售后流程，不是终态 5
# 所以断言不能写死 5，否则在实物类商品上会误报失败（踩过：订单 72880177）。
# 口径在 client.py 的 REFUNDED_STATUS 里统一维护，别在这里另写一份。
REFUND_ORDER_STATUS = {s: ORDER_STATUS_TEXT.get(s, "?") for s in sorted(REFUNDED_STATUS)}

pytestmark = [
    pytest.mark.refund,
    pytest.mark.release_core,
    pytest.mark.skipif(
        WHAT not in WHAT_CHOICES,
        reason=f"MIGU_REFUND_WHAT={WHAT!r} 非法，可选 {sorted(WHAT_CHOICES)}",
    ),
    # 生产写操作闸门：退款会真的退钱、改订单状态，生产默认拦掉
    pytest.mark.skipif(prod_write_blocked(), reason=prod_write_skip_reason()),
]


# 退款只允许操作**白名单账号**的订单。
# 店铺里同时存在大量其他用户的真实订单（生产 storeId=888 也不例外），
# 自动找单必须按订单归属过滤，绝不能"看到可退就退"。
# 白名单直接复用 config 里登记的账号：测试 15550042537 / 生产 18366267528 等。
ALLOWED_MOBILES = set(env.accounts)


def _is_our_order(row: dict) -> bool:
    """订单是否属于白名单账号（B 端列表里带 userMobileNo）。"""
    return str(row.get("userMobileNo") or "") in ALLOWED_MOBILES


# 用 MIGU_REFUND_PARTIAL_ORDER 显式指定的那笔订单是**留给部分退用例**的，
# 全额退用例的自动找单必须绕开它 —— 否则两套用例抢同一笔单：
# 全额退会把它退成终态，部分退就没得退了（踩过）。
RESERVED_FOR_PARTIAL = (
    {str(os.environ["MIGU_REFUND_PARTIAL_ORDER"])}
    if os.environ.get("MIGU_REFUND_PARTIAL_ORDER") else set()
)


def _trial_amount(rc: RefundClient, oid, items, amount) -> float | None:
    """只读试算：返回系统认为"这次该退多少"；接口报错返回 None。"""
    try:
        calc = rc.calculate(oid, items, what=WHAT, amount=amount)
    except BAdminError:
        return None
    value = calc.get("actualRefundAmount")
    return float(value) if value is not None else None


def _find_refundable(rc: RefundClient) -> tuple:
    """找一笔可退订单：优先有金额的（真资金链路），其次任意可退的。

    ⚠️ 只挑**白名单账号**的订单。返回 (orderId, 订单快照, 可退性, refundItems)；
    找不到返回四个 None。
    """
    explicit = os.environ.get("MIGU_REFUND_ORDER")
    if explicit:
        info = rc.refundable(explicit)
        return int(explicit), rc.get_order(explicit), info, rc.build_items(info, what=WHAT)

    paid_candidate = None
    fallback = None
    for order_status in (11, 7, 0):
        for row in (rc.list_orders(order_status=order_status, page_size=30).get("data") or []):
            if not _is_our_order(row):
                continue                      # 别人的订单，绝对不碰
            oid = row.get("orderId")
            if str(oid) in RESERVED_FOR_PARTIAL:
                continue                      # 留给部分退用例，别抢
            try:
                info = rc.refundable(oid)
            except BAdminError:
                continue                      # 未支付/不存在，跳过
            items = rc.build_items(info, what=WHAT)
            if not items:
                continue
            # ★ 自校验：试算金额必须与"还可退金额"对得上，否则这笔单退不出预期金额，
            #   拿去做全额退闭环必在"实退 == 可退"那一步失败。提前筛掉，别浪费一笔单：
            #     ① 单价 1 分的小额单，退 1 份会被分摊取整抹成 0（见 partial_ctx 注释）
            #     ② 历史"按金额部分退"留下的账实不符脏单（remainCount 还有，钱已为 0）
            expect = float(info.get("remainRefundableAmount") or 0)
            trial = _trial_amount(rc, oid, items, expect)
            if trial is None or abs(trial - expect) >= 1e-6:
                continue
            if paid_candidate is None and float(info.get("orderPaidAmount") or 0) > 0:
                paid_candidate = (int(oid), row, info, items)
            elif fallback is None:
                fallback = (int(oid), row, info, items)
        if paid_candidate:
            break
    return paid_candidate or fallback or (None, None, None, None)


def _choose_partial_count(rc: RefundClient, oid, info, iid, remain: int,
                          unit: float) -> int | None:
    """挑一个"退得动钱"的部分退份数（1 <= n <= remain-1），挑不到返回 None。

    ★ 为什么必须挑：单价 1 分的小额单退 1 份会被系统按比例分摊抹成 0 元
      （实测 0.01 元/份 × 3 份：退 1 份 0.00、退 2 份 0.02、退 3 份 0.03）。
      这不是功能坏了，是取整精度；但不挑就会造出**与功能无关的假失败**。
    顺序：先试 `MIGU_REFUND_PARTIAL_N`（首选），再往多退，最后往少退。

    `unit <= 0`（0 元夹具单）直接返回首选份数：没有金额就没有"分摊取整"，
    试算恒为 0，用"试算 > 0"去挑会一个都挑不到。
    """
    if unit <= 0:
        return min(max(1, PARTIAL_N), remain - 1)
    candidates = [
        n for n in (PARTIAL_N, *range(PARTIAL_N + 1, remain), *range(PARTIAL_N - 1, 0, -1))
        if 1 <= n <= remain - 1
    ]
    for n in candidates:
        trial = _trial_amount(rc, oid, rc.build_items(info, what=WHAT, counts={iid: n}),
                              round(unit * n, 2))
        if (trial or 0) > 0:
            return n
    return None


# ===== 0 元多份夹具单：让「部分退」进得了全量回归，且不用真人付款 =====
# 为什么需要：部分退要求"某可退项 remainCount >= 2"。店铺里现成的多份单要么属于
#   其他账号（硬约束：只碰白名单），要么已被退成账实不符的脏数据；而下一笔**付费**
#   多份单要真人扫码，且测试环境收银台只给 118 秒 —— 实测 72890316 就是这么翻的：
#   钱扣了（paySuccess=true），订单却在超时那一刻被自动关闭（4），钱拿不回来。
# 做法：用环境的 **0 元夹具商品**（`fixture_free_product`，下单即出票 status=11）自造一笔。
#
# ⚠️ 代价（说清楚，别误读报告）：0 元单没有资金流动，只能验证**份数 / 订单状态**维度
#    （refundedCount/remainCount 正确扣减、订单仍可继续退）。金额维度由真金用例单独覆盖：
#    测试 72890269 退 2 份真退 0.02、生产 61008486 / 61007604 退 2 份真退 0.02。
# ⚠️ 生产**绝不能**启用（`env.supports_free_multi_fixture=False`），理由见 config.py：
#    生产 0 元单部分退是**绿**的、有价单才是**红**的，用 0 元单会把已知 bug 盖掉。
FREE_FIXTURE_COUNT = max(2, int(os.environ.get("MIGU_FREE_FIXTURE_COUNT", "3")))


def _provision_free_multi_order(rc: RefundClient) -> tuple:
    """下一笔 0 元多份夹具单（下单即成"已付款"），返回 (orderId, 订单快照, 可退性, 目标项)。

    失败一律返回四个 None，交给调用方照常 skip —— 造不出夹具不该报成功能失败。
    """
    # 目标从「测试专用商品清单」按用途取（usage=free_fixture），不写死 ID
    try:
        target = catalog.one(usage="free_fixture")
    except catalog.CatalogError:
        return (None, None, None, None)
    pid, sku = target.product_id, target.sku_id
    try:
        client = MiguClient()
        client.login()
        oid = client.create_order(pid, sku, FREE_FIXTURE_COUNT, target.price,
                                  source="test_b_refund._provision_free_multi_order")
    except Exception:                     # noqa: BLE001 —— 造夹具失败不该红
        return (None, None, None, None)
    # 0 元单的支付记录同步到 B 端有秒级延迟（实测约 8s）：先等到它变成"可退"
    info = None
    for _ in range(8):
        try:
            info = rc.refundable(oid)
            break
        except BAdminError:
            time.sleep(5)
    if not info:
        return (None, None, None, None)
    items = info.get("refundableItems") or []
    target = max(items, key=lambda x: int(x.get("remainCount") or 0)) if items else None
    if not target or int(target.get("remainCount") or 0) < 2:
        return (None, None, None, None)
    return int(oid), rc.get_order(oid), info, target


# ===== 付费多份夹具单：闸门打开时"下新单 + 等真人扫码"，把资金维度也覆盖上 =====
# 为什么需要：生产的部分退 bug **只在带资金的退款分支上出现** ——
#   0 元单部分退会正确落 15(部分退)（假绿），有价单才会错误落 5(已退款)（真红）。
#   所以生产要想覆盖这条，必须有一笔**真付过钱**的多份单。
# 谁来决定：`--real-pay=on` / MIGU_REAL_PAY=1（见 migu_api/fundgate.py）。
#   闸门关着 → 不花一分钱（测试环境退化为 0 元夹具单/或跳过；生产跳过）。
#   闸门打开 → 自动下单、打印二维码、**阻塞等你扫**，付完接着把用例跑完。
ARTIFACTS = Path(__file__).parent / "artifacts"
CHANNEL = os.environ.get("MIGU_PAY_CHANNEL", env.pay_bank_code).upper()
PAID_FIXTURE_COUNT = max(2, int(os.environ.get("MIGU_PAID_FIXTURE_COUNT", "3")))
# 付费夹具单的产物（二维码路径等），供用例写进 Allure 报告当付款证据
_FIXTURE_META: dict = {}


def _provision_paid_multi_order(rc: RefundClient) -> tuple:
    """**下一笔付费多份单并等真人扫码付款**，返回 (orderId, 订单快照, 可退性, 目标项)。

    只在真实支付闸门 = `on` 时会被调用（见 `_find_multi_refundable`）。
    任何一步失败都返回四个 None —— 造不出夹具照常 skip，不能报成功能失败。
    """
    # 目标从「测试专用商品清单」按用途取（usage=paid = 全清单唯一主支付目标）
    try:
        target = catalog.one(usage="paid")
    except catalog.CatalogError:
        return (None, None, None, None)
    pid, sku, unit = target.product_id, target.sku_id, target.price
    count = PAID_FIXTURE_COUNT
    total = f"{round(float(unit) * count, 2):.2f}"

    try:
        client = MiguClient()
        client.login()
        oid = client.create_order(pid, sku, count, total,
                                  source="test_b_refund._provision_paid_multi_order")
        pay_detail = client.get_pay_detail(oid)
        pay = client.create_payment(oid, bank_code=CHANNEL, pay_amount=total)
        params = pay.get("paymentParams") or ""
        assert params, "未返回支付参数"
        ensure_dir(ARTIFACTS)
        form_html = None
        if paypage.is_link(params):
            pay_url = paypage.strip_cdata(params)
        else:
            form_html = paypage.save_form_html(
                params, ARTIFACTS / f"{env.name}_partial_fixture_{oid}_form.html")
            pay_url = paypage.to_pay_url(params)
        png = qr.save_png(pay_url, ARTIFACTS / f"{env.name}_partial_fixture_{oid}_qr.png")
    except Exception as exc:              # noqa: BLE001 —— 造夹具失败不该红
        print(f"  ✗ 付费多份单造单失败：{type(exc).__name__}: {str(exc)[:160]}")
        return (None, None, None, None)

    left = int(pay_detail.get("leftTime") or 0) // 1000
    _FIXTURE_META.update({"qrPng": str(png), "payUrl": pay_url,
                          "formHtml": str(form_html) if form_html else None,
                          "orderId": oid, "count": count, "total": total,
                          "channel": CHANNEL})
    timeout = fundgate.pay_timeout()
    print(f"\n{'=' * 68}\n【部分退夹具】请扫码支付 {total} 元（{count} 份，环境 {env.name}）\n"
          f"二维码：{png}\n"
          + (f"表单页：{form_html}\n" if form_html else "")
          + f"收银台倒计时 {left}s；本用例最多等你 {timeout}s\n{'=' * 68}\n", flush=True)

    if not client.wait_for_paid(oid, timeout=timeout, interval=3, verbose=True):
        print(f"  ✗ 等待超时，订单 {oid} 仍未支付（夹具作废）")
        return (None, None, None, None)

    # 支付记录同步到 B 端可退索引有秒级延迟（实测约 8s）
    info = None
    for _ in range(10):
        try:
            info = rc.refundable(oid)
            break
        except BAdminError:
            time.sleep(4)
    if not info:
        return (None, None, None, None)
    items = info.get("refundableItems") or []
    target = max(items, key=lambda x: int(x.get("remainCount") or 0)) if items else None
    if not target or int(target.get("remainCount") or 0) < 2:
        return (None, None, None, None)
    _FIXTURE_META["paid"] = True
    print(f"  ✓ 夹具单 {oid} 已支付，可退 {target.get('remainCount')} 份", flush=True)
    return int(oid), rc.get_order(oid), info, target


def _find_multi_refundable(rc: RefundClient, exclude=frozenset()) -> tuple:
    """找一笔**可部分退**的订单：某可退项 remainCount >= 2 且"退得动钱"。

    ⚠️ 四条硬约束（都踩过）：
      1. **只挑白名单账号的订单**（`userMobileNo` 必须登记在 `env.accounts`）——
         店铺里同时有大量其他用户的真实订单，绝不能被自动化退掉。
      2. **账实相符**：剩余金额 == 单价 × 剩余份数。历史上"按金额部分退"留下过
         不一致数据（例如 remainCount=2 但 remain=0.03），在那上面按份试算会算出 0，
         属数据问题而非功能问题，混进来会让结论失真。
      3. **退得动钱**：存在某个份数 n（1 <= n <= remain-1）试算额 > 0（见
         `_choose_partial_count`）。否则选中它只会在后面报一个与功能无关的**假失败**
         （典型：剩 2 份 × 0.01，退 1 份取整成 0、退 2 份就是退光）。
      4. 列表里 `count` 就能看出件数，先按它预筛（< 2 直接跳过），省掉一次
         `refundable` 调用——店铺订单上千笔，这点很关键。

    exclude 用于避开全额退用例已占用的订单（两套用例不能共用一笔单）。
    返回 (orderId, 订单快照, 可退性, 目标项)；找不到返回四个 None。
    """
    explicit = os.environ.get("MIGU_REFUND_PARTIAL_ORDER")
    if explicit:
        info = rc.refundable(explicit)
        items = info.get("refundableItems") or []
        target = max(items, key=lambda x: int(x.get("remainCount") or 0)) if items else None
        return int(explicit), rc.get_order(explicit), info, target

    for order_status in (11, 8, 14, 15, 5, 7, 0):
        for page_index in (1, 2, 3):
            rows = (rc.list_orders(order_status=order_status, page_size=50,
                                   page_index=page_index).get("data") or [])
            if not rows:
                break
            for row in rows:
                oid = row.get("orderId")
                if any(str(oid) == str(x) for x in exclude):
                    continue
                if not _is_our_order(row):
                    continue                  # 别人的订单，绝对不碰
                if int(row.get("count") or 1) < 2:
                    continue                  # 只有 1 份，拆不开
                try:
                    info = rc.refundable(oid)
                except BAdminError:
                    continue                  # 未支付/不存在，跳过
                items = info.get("refundableItems") or []
                if not items:
                    continue
                target = max(items, key=lambda x: int(x.get("remainCount") or 0))
                remain = int(target.get("remainCount") or 0)
                unit = float(target.get("unitPrice") or 0)
                remain_amount = float(info.get("remainRefundableAmount") or 0)
                if remain < 2 or unit <= 0 or remain_amount <= 0:
                    continue
                if abs(remain_amount - round(unit * remain, 2)) >= 1e-6:
                    continue                  # 账实不符（历史脏数据），跳过
                if _choose_partial_count(rc, oid, info, target.get("orderItemId"),
                                         remain, unit) is None:
                    continue                  # 退不动钱，做了也是假失败
                return int(oid), row, info, target

    # 店铺里没有"可部分退"的现成订单，按闸门决定怎么补：
    #   ① 闸门 = on（--real-pay=on）→ 下一笔**付费**多份单并等真人扫码（资金维度，
    #      生产靠它才能暴露那个 bug；测试环境同样有效，只是要花钱）
    #   ② 否则环境允许就自造 **0 元**多份夹具单（免费，但只有份数/状态维度）
    #   ③ 都不行 → 返回 None，用例跳过
    if fundgate.allows_new_payment():
        got = _provision_paid_multi_order(rc)
        if got[0] is not None:
            return got
    if env.supports_free_multi_fixture:
        return _provision_free_multi_order(rc)
    return (None, None, None, None)


@pytest.fixture(scope="module")
def refund_ctx():
    """模块级：一个退款客户端 + 一笔确定的可退订单（订单只能退一次，必须共用）。"""
    rc = RefundClient()
    # 会话前置检查：别等提交那一步才发现 B 端掉线，白跑一趟
    total = rc.list_orders(page_size=1).get("totalNum")
    assert total is not None, "B 端会话不可用（拿不到订单总数）——先跑 ./run.sh ticket ensure"

    oid, row, info, items = _find_refundable(rc)
    if oid is None:
        pytest.skip(
            f"环境 [{env.name}] 店铺 {rc.store_id} 里没有可退订单了。"
            "用 MIGU_REFUND_ORDER=<orderId> 指定一笔已付订单，"
            "或先在 C 端下单支付一笔（./run.sh realpay）"
        )
    allure.attach(json.dumps(
        {"env": env.name, "storeId": rc.store_id, "orderId": oid,
         "orderStatusName": (row or {}).get("statusName"), "what": WHAT_LABEL},
        ensure_ascii=False, indent=1), name="本轮目标",
        attachment_type=allure.attachment_type.JSON)
    return {"rc": rc, "orderId": oid, "row": row, "refundable": info, "items": items}


@pytest.fixture(scope="module")
def partial_ctx(refund_ctx):
    """模块级：部分退用的一笔订单（**必须与全额退那笔不同**，否则互相干扰）。

    挑选条件：某个可退项的 remainCount >= 2，且本次退的份数 < remainCount。
    """
    rc = refund_ctx["rc"]
    # 复用全额退用例的客户端（同一会话），但订单必须错开
    oid, row, info, target = _find_multi_refundable(rc, exclude={refund_ctx["orderId"]})
    if oid is None or not target:
        if fundgate.allows_new_payment():
            hint = ("\n\n闸门已是 on：本用例已尝试自动下一笔付费多份单并等你扫码，"
                    "但没能拿到可部分退的多份单（多半是等待超时或订单被关闭）。"
                    "可直接指定一笔已付订单：MIGU_REFUND_PARTIAL_ORDER=<orderId>")
        else:
            hint = "\n\n" + fundgate.skip_reason("部分退（需要一笔 ≥2 份的已付订单）")
        pytest.skip(
            f"环境 [{env.name}] 店铺 {rc.store_id} 里没有「可部分退」的订单"
            f"（需要某可退项 remainCount >= 2 且还有钱可退）。"
            "可用 MIGU_REFUND_PARTIAL_ORDER=<orderId> 指定一笔多份订单；"
            "或在 C 端下一笔数量 >= 2 的订单再支付" + hint
        )

    remain = int(target.get("remainCount") or 0)
    unit = float(target.get("unitPrice") or 0)
    iid = target.get("orderItemId")
    # 退的份数必须严格小于 remain，否则就不是"部分退"了。
    # ★ 还要求"退得动钱"：小额订单（单价 1 分）退 1 份会被系统按比例分摊抹成 0 元 ——
    #   实测 0.01 元/份 × 3 份的单：退 1 份试算 0.00、退 2 份 0.02、退 3 份 0.03。
    #   这不是部分退功能坏了，是取整精度；用例主动换份数，别把精度问题误报成功能缺陷。
    count = _choose_partial_count(rc, oid, info, iid, remain, unit)
    if count is None:
        # 用 skip 而不是 assert：这是"环境里没有可用订单"，不是被测功能出错，
        # 报成红会把真正的功能 bug 淹掉。
        pytest.skip(
            f"环境 [{env.name}] 里可部分退的订单（{oid}，剩 {remain} 份 × 单价 {unit}）"
            f"退任何份数试算都是 0 元 —— 是分摊取整，验证不了部分退金额。"
            "请换一笔单价更大的订单（MIGU_REFUND_PARTIAL_ORDER=<orderId>）"
        )
    submit_amount = round(unit * count, 2)
    remain_amount = float(info.get("remainRefundableAmount") or 0)
    # 账实相符 = 剩余可退金额正好等于 单价 × 剩余份数（断言可以写得更硬）
    consistent = abs(remain_amount - round(unit * remain, 2)) < 1e-6
    # 0 元夹具单（无资金流动）：只验证份数/状态维度，金额断言一律放宽（见
    # `_provision_free_multi_order` 的说明）。真金用例负责金额维度。
    zero_money = unit <= 0
    if not zero_money:
        assert remain_amount > 0, f"订单剩余可退金额为 0，无法验证部分退金额"

    allure.attach(json.dumps(
        {"env": env.name, "storeId": rc.store_id, "orderId": oid,
         "orderStatusName": (row or {}).get("statusName"),
         "itemId": target.get("orderItemId"), "skuId": target.get("skuId"),
         "remainCount": remain, "unitPrice": unit, "remainAmount": remain_amount,
         "本次退份数": count, "提交金额(单价×份数)": submit_amount, "账实相符": consistent,
         "0元夹具单(无资金维度)": zero_money,
         "真实支付闸门": fundgate.mode(),
         "本单真人付款": bool(_FIXTURE_META.get("paid"))},
        ensure_ascii=False, indent=1), name="部分退本轮目标",
        attachment_type=allure.attachment_type.JSON)
    return {"rc": rc, "orderId": oid, "row": row, "refundable": info,
            "targetItem": target, "count": count, "unitPrice": unit,
            "submitAmount": submit_amount, "remainBefore": remain,
            "remainAmount": remain_amount, "consistent": consistent,
            "zeroMoney": zero_money,
            "paidByHuman": bool(_FIXTURE_META.get("paid")),
            "qrPng": _FIXTURE_META.get("qrPng")}


# 退款工单成功后，订单/可退性会先进入**过渡态**：实测生产环境在工单成功瞬间仍能读到
# 「remainCount 已扣减 + 14(售后中)」的假象，几秒后才落终态（5 已退款、剩余份数不可再退）。
# 断言必须基于终态，否则会把过渡态误当结果 —— 这个坑曾让生产的部分退 bug 一度"跑绿"。
SETTLE_SECONDS = float(os.environ.get("MIGU_REFUND_SETTLE_SECONDS") or 10)


def _settle_refundable(rc, oid, min_wait=None, interval=2.0, timeout=45):
    """等订单/可退性落库稳定，返回 (最后一次快照, 错误信息)。

    稳定 = 至少等过 `min_wait` 秒，且连续两次读到的关键字段完全一致。
    稳定后仍查不到（BAdminError）则快照为 None、错误信息带回给调用方。
    """
    min_wait = SETTLE_SECONDS if min_wait is None else min_wait
    start = time.time()
    last_info, last_err, sig, prev_sig = None, None, None, object()
    while time.time() - start < timeout:
        try:
            last_info = rc.refundable(oid)
            last_err = None
            sig = (last_info.get("remainRefundableAmount"), tuple(
                (x.get("orderItemId"), x.get("remainCount"), x.get("refundedCount"))
                for x in (last_info.get("refundableItems") or [])))
        except BAdminError as exc:
            last_info, last_err = None, getattr(exc, "message", str(exc))
            sig = ("ERR", last_err)
        if time.time() - start >= min_wait and sig == prev_sig:
            break
        prev_sig = sig
        time.sleep(interval)
    return last_info, last_err


@allure.feature("退款")
@allure.story("可退性")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("可退性查询：金额构成与可退项正确")
def test_refundable_info(refund_ctx):
    """只读第一步：订单可退、金额能对上、票据可退标记为真。"""
    info = refund_ctx["refundable"]
    with allure.step("核对可退金额构成"):
        allure.attach(json.dumps(info, ensure_ascii=False, indent=1), name="可退性原始返回",
                      attachment_type=allure.attachment_type.JSON)
    paid = float(info.get("orderPaidAmount") or 0)
    refunded = float(info.get("totalRefundedAmount") or 0)
    remain = float(info.get("remainRefundableAmount") or 0)
    allure.attach(f"实付={paid} 已退={refunded} 还可退={remain}", name="金额")
    assert abs(remain - (paid - refunded)) < 1e-6, "还可退金额 ≠ 实付 − 已退"
    if paid > 0:
        assert remain > 0, "有实付金额的订单，还可退金额不应为 0"
    else:
        # 0 元夹具单（下单即出票、没有资金流水）：可退金额本来就是 0，
        # 这条链路只验"票能不能退"，钱的部分由有实付金额的订单覆盖
        allure.attach("0 元订单：无可退金额，只校验票据可退", name="说明")

    with allure.step("核对待退项"):
        first = (info.get("refundableItems") or [{}])[0]
        allure.attach(json.dumps(info.get("refundableItems"), ensure_ascii=False),
                      name="refundableItems")
    assert info.get("refundableItems"), "可退项为空"
    assert first.get("remainCount", 0) >= 1, "可退数量为 0"
    assert first.get("canRefundTicket") is True, "票据标记为不可退"


@allure.feature("退款")
@allure.story("试算")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("退款试算：金额与可退金额一致（只读，不改数据）")
def test_refund_calculate(refund_ctx):
    """试算是**只读**接口，正式提交前先确认"系统认为该退多少"。"""
    rc, info, items = refund_ctx["rc"], refund_ctx["refundable"], refund_ctx["items"]
    with allure.step(f"试算（退什么={WHAT_LABEL}）"):
        calc = rc.calculate(refund_ctx["orderId"], items, what=WHAT,
                            amount=info.get("remainRefundableAmount"))
        allure.attach(json.dumps(calc, ensure_ascii=False, indent=1), name="试算结果",
                      attachment_type=allure.attachment_type.JSON)
    assert calc.get("actualRefundAmount") is not None, f"试算未返回退款金额：{calc}"
    diff = abs(float(calc.get("actualRefundAmount") or 0)
               - float(info.get("remainRefundableAmount") or 0))
    assert diff < 1e-6, "试算金额与可退金额不一致"
    refund_ctx["calc"] = calc


@allure.feature("退款")
@allure.story("防呆")
@allure.severity(allure.severity_level.NORMAL)
@allure.title("非法退款类型被拒绝（refundType 只接受 2=部分退款）")
def test_invalid_refund_type_rejected(refund_ctx):
    """反例：`/refund/partial` 的 refundType 只接受 2，传业务类型码必须被拒。

    这一条专门防呆：文档里 `refundType` **没有任何枚举说明**，
    很容易按"退票(1)/退款(2)/退票+退款(3)"的直觉去传，从而误判成"退款接口有缺陷"。
    实测传 1/3/4/5 一律 500 "退款类型必须为部分退款"。
    """
    rc, info, items = refund_ctx["rc"], refund_ctx["refundable"], refund_ctx["items"]
    # 先记下这笔单**已有的**工单，用来判"防呆用例有没有留下副作用"（见下方断言）
    tasks_before = {str(t.get("taskId")) for t in
                    (rc.list_tasks(order_id=refund_ctx["orderId"], page_size=20).get("data") or [])}
    payload = rc._refund_payload(refund_ctx["orderId"], items, WHAT,
                                 info.get("remainRefundableAmount"), "防呆用例", "negative")
    with allure.step("把 refundType 改成 3（直觉上的「退票+退款」）提交，期望被拒"):
        payload["refundType"] = 3
        with pytest.raises(BAdminError) as exc:
            rc._request("POST", "/v1/shopTrade/refund/partial", json=payload)
        msg = getattr(exc.value, "message", "") or ""
        allure.attach(f"code={getattr(exc.value, 'code', None)} message={msg}", name="拒绝原因")
    assert "退款类型" in msg, f"预期按退款类型拒绝，实际：{msg}"
    # 防呆用例不能有副作用：不能**新增**工单。
    # ⚠️ 别断言"工单列表为空"：订单上本来就可能已有历史工单（早先被退过一部分、
    #    或人工验证过），"为空"会报一个与防呆无关的假失败（踩过）。判据是**前后差集**。
    tasks_after = {str(t.get("taskId")) for t in
                   (rc.list_tasks(order_id=refund_ctx["orderId"], page_size=20).get("data") or [])}
    assert tasks_after <= tasks_before, (
        f"防呆用例产生了新工单：{sorted(tasks_after - tasks_before)}"
    )


@allure.feature("退款")
@allure.story("退款闭环")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("退款闭环：提交 → 审批 → 工单成功 → 订单变已退款")
def test_refund_closed_loop(refund_ctx):
    """全链路：只有"工单真的成功 + 订单真的变成已退款"才算通过。"""
    rc = refund_ctx["rc"]
    oid = refund_ctx["orderId"]
    info = refund_ctx["refundable"]
    items = refund_ctx["items"]

    # ---------- 1. 提交退款申请 ----------
    with allure.step(f"提交退款申请（退什么={WHAT_LABEL}）"):
        refund_id = rc.submit(oid, items, what=WHAT,
                              amount=info.get("remainRefundableAmount"),
                              reason="自动化退款验证", remark="test_b_refund")
        allure.attach(str(refund_id), name="refundId（整数记录ID，不是 taskId）")
    assert isinstance(refund_id, int), f"/refund/partial 应返回整数 refundId，实际 {refund_id!r}"

    # ---------- 2. 回查工单拿 taskId ----------
    with allure.step("回查工单列表拿 taskId（审批要 refundId + taskId 两个都传）"):
        task = rc.find_task(oid, timeout=30)
        assert task, f"提交成功(refundId={refund_id})但按订单号回查不到工单"
        task_id = task.get("taskId")
        allure.attach(json.dumps(task, ensure_ascii=False, indent=1), name="工单快照",
                      attachment_type=allure.attachment_type.JSON)
        allure.attach(task_status_desc(task.get("taskStatus")), name="工单初始状态")
    assert task_id, f"工单没有 taskId：{task}"

    # ---------- 3. 审批 ----------
    with allure.step("审批退款工单（approvalStatus=2 = 同意）"):
        rc.approval(refund_id, task_id)
        allure.attach(
            "approvalStatus=2 —— 实测唯一可用值，语义是「同意」而不是「驳回」；"
            "rejectReason 必须非空（后端 DTO 标了 @NotBlank，连同意也要填）",
            name="审批入参说明")

    # ---------- 4. 等工单到成功 ----------
    with allure.step("轮询工单直到成功(taskStatus=2)"):
        snap = rc.wait_task(task_id, timeout=90)
        allure.attach(json.dumps(snap, ensure_ascii=False, indent=1), name="工单终态",
                      attachment_type=allure.attachment_type.JSON)
    assert snap and snap.get("taskStatus") == TASK_SUCCESS, (
        f"工单未到成功，实际 {task_status_desc((snap or {}).get('taskStatus'))}"
    )
    assert snap.get("statusDesc") == "同意", f"审批结果应为同意，实际 {snap.get('statusDesc')}"
    assert int(snap.get("taskSuccessCount") or 0) >= 1, "成功条数为 0"
    assert int(snap.get("taskFailCount") or 0) == 0, f"存在失败条数：{snap.get('taskFailCount')}"

    with allure.step("核对实退金额与退什么"):
        actual = float(snap.get("actualRefundAmount") or 0)
        expected = float(info.get("remainRefundableAmount") or 0)
        label = what_desc(snap.get("hasTicketRefund"), snap.get("hasMoneyRefund"))
        allure.attach(f"实退={actual} 预期={expected} 退什么={label}", name="资金结果")
    assert abs(actual - expected) < 1e-6, f"实退金额 {actual} ≠ 可退金额 {expected}"
    assert snap.get("hasTicketRefund") == WANT_TICKET, "退票标记与预期不符"
    assert snap.get("hasMoneyRefund") == WANT_MONEY, "退款标记与预期不符"

    # ---------- 5. 订单进入退款后的状态 ----------
    with allure.step(f"校验订单状态进入退款后状态 {sorted(REFUND_ORDER_STATUS)}"):
        deadline = time.time() + 30
        order = None
        while time.time() < deadline:
            order = rc.get_order(oid)
            if order and order.get("status") in REFUND_ORDER_STATUS:
                break
            time.sleep(2)
        allure.attach(json.dumps(order, ensure_ascii=False, indent=1) if order else "查不到",
                      name="订单快照", attachment_type=allure.attachment_type.JSON)
        allure.attach(
            "电子票类商品退款后订单 → 5(已退款)；实物/自取类 → 14(售后中)。"
            "两者都是「退款已受理」的合法结果，权威结论看工单 taskStatus=2。",
            name="状态口径说明")
    assert order, f"退款完成后 B 端查不到订单 {oid}"
    status = order.get("status")
    assert status in REFUND_ORDER_STATUS, (
        f"订单应进入退款后状态 {sorted(REFUND_ORDER_STATUS)}，"
        f"实际 {status}({order.get('statusName')})"
    )

    # ---------- 6. 反向断言：全额退款后不可再退 ----------
    with allure.step("反向断言：已退款订单不再可退"):
        try:
            again = rc.refundable(oid)
        except BAdminError as exc:
            again = {"error": getattr(exc, "message", str(exc))}
        allure.attach(json.dumps(again, ensure_ascii=False), name="二次可退性查询")
    blocked = "error" in again or float(again.get("remainRefundableAmount") or 0) == 0
    assert blocked, f"已退款订单竟然还可退：{again}"

    # 落一份结果，方便人工核对
    artifacts = os.path.join(os.path.dirname(__file__), "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    result = {"env": env.name, "orderId": oid, "refundId": refund_id, "taskId": task_id,
              "taskStatus": snap.get("taskStatus"), "statusDesc": snap.get("statusDesc"),
              "actualRefundAmount": actual, "what": WHAT,
              "orderStatusAfter": order.get("status"),
              "orderStatusName": order.get("statusName")}
    with open(os.path.join(artifacts, f"{env.name}_last_refund.json"), "w",
              encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    allure.attach(json.dumps(result, ensure_ascii=False, indent=1), name="本轮结果",
                  attachment_type=allure.attachment_type.JSON)


# ============================================================================
# 部分退款（只退其中若干份，剩余仍可退）
# ============================================================================

def _calc_items(rc, oid, it, ticket_count, amount):
    """按指定份数试算（只读）。"""
    return rc.calculate(oid, [{
        "orderItemId": it.get("orderItemId"), "skuId": it.get("skuId"),
        "refundTicketCount": ticket_count, "refundMoneyCount": ticket_count,
        "itemOriginalAmount": amount,
    }], what=WHAT, amount=amount)


@allure.feature("退款")
@allure.story("部分退")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("部分退契约：退得少 → 退得少钱（金额随份数递减，不多退）")
def test_partial_amount_contract(partial_ctx):
    """只读试算，把部分退的核心契约钉死（零副作用、零成本，可反复跑）。

    实测结论：
      - `itemOriginalAmount` 是**金额控制点**（系统把它当"本次选中项金额"）；
      - `refundTicketCount/refundMoneyCount` 只决定"记几份"，**不影响金额**；
      - 而且**金额会按订单优惠分摊**，不完全等于 `单价 × 份数`
        （实测 1.1 元/份的订单，退 1 份试算得 1.09）。
        所以"退多少钱"一律以**试算结果**为准，不能拿 单价×份数 去硬断言。
    """
    rc = partial_ctx["rc"]
    oid = partial_ctx["orderId"]
    it = partial_ctx["targetItem"]
    unit = partial_ctx["unitPrice"]
    count = partial_ctx["count"]
    remain = partial_ctx["remainBefore"]

    with allure.step("本轮夹具单来源（付费 vs 0 元）"):
        allure.attach(fundgate.label(), name="真实支付闸门")
        if partial_ctx["paidByHuman"]:
            # 闸门 on 下，用例自己下了一笔付费多份单并等你扫码 —— 二维码留档
            allure.attach(
                f"订单 {oid}：用例自动下单（{count} 份）并由**真人扫码付款**，"
                "因此本轮带资金维度（金额断言有效）。",
                name="夹具说明", attachment_type=allure.attachment_type.TEXT)
            if partial_ctx.get("qrPng"):
                allure.attach.file(partial_ctx["qrPng"], name="本单付款二维码",
                                   attachment_type=allure.attachment_type.PNG)
        else:
            allure.attach(
                "本轮订单来自门店现成可退单或 0 元夹具单（无真人付款动作）",
                name="夹具说明", attachment_type=allure.attachment_type.TEXT)

    with allure.step(f"试算：只退 {count} 份（{round(unit * count, 2)} 元）"):
        by_partial = _calc_items(rc, oid, it, count, round(unit * count, 2))
        allure.attach(json.dumps(by_partial, ensure_ascii=False), name="按部分份数试算")
    with allure.step(f"试算：退满 {remain} 份（{round(unit * remain, 2)} 元）"):
        by_whole = _calc_items(rc, oid, it, remain, round(unit * remain, 2))
        allure.attach(json.dumps(by_whole, ensure_ascii=False), name="按整项份数试算")

    part_amt = float(by_partial.get("actualRefundAmount") or 0)
    whole_amt = float(by_whole.get("actualRefundAmount") or 0)
    allure.attach(
        f"退 {count} 份 → {part_amt} 元；退满 {remain} 份 → {whole_amt} 元。"
        "退得少必须退得少钱，且不能超过 单价×份数（防止把整项的钱都退掉）。",
        name="契约结论")

    if partial_ctx["zeroMoney"]:
        # 0 元夹具单：没有资金流动，"退得少退得少钱"在本轮不适用（试算恒为 0）。
        # 只保留"不多退"的上限与"退满 == 整单可退"两条守卫，份数维度由闭环用例覆盖。
        allure.attach(
            "本轮用的是 0 元夹具单（无资金流动），金额维度不适用；"
            "金额契约由真金用例单独覆盖（测试 72890269 / 生产 61008486 均真退 0.02）。",
            name="口径说明")
        assert part_amt == 0 and whole_amt == 0, (
            f"0 元单试算应恒为 0，实际 部分={part_amt} 整项={whole_amt}"
        )
        assert part_amt <= round(unit * count, 2) + 0.01
        assert abs(whole_amt - partial_ctx["remainAmount"]) < 0.02
        return

    assert part_amt > 0, "部分退试算金额为 0（该订单没有可退金额）"
    assert part_amt < whole_amt, (
        f"退 {count} 份（{part_amt}）没有比退满 {remain} 份（{whole_amt}）少 —— "
        "部分退未生效，可能把整项都退了"
    )
    assert part_amt <= round(unit * count, 2) + 0.01, (
        f"退 {count} 份的试算金额 {part_amt} 超过 单价×份数 {round(unit * count, 2)}，存在多退"
    )
    # 退满时必须等于整单可退额（判定口径以测试环境为准）。
    # 旧实现把单价当 itemOriginalAmount，多份订单会只退 1 份的钱，这条就是它的回归防线。
    assert abs(whole_amt - partial_ctx["remainAmount"]) < 0.02, (
        f"退满试算 {whole_amt} 与整单可退 {partial_ctx['remainAmount']} 不符 —— "
        "多份订单的全额退金额算错了"
    )


@allure.feature("退款")
@allure.story("部分退")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("部分退闭环：只退部分份数 → 工单成功 → 剩余份数仍可退")
def test_partial_refund_closed_loop(partial_ctx):
    """核心：只退 N 份，钱按 N 份退，**剩余份数保留且仍可继续退**。

    与全额退用例的本质区别：退完后订单**没有**变成"已退款终态"，
    `remainCount > 0`、`remainRefundableAmount > 0`，还能再退一次。
    """
    rc = partial_ctx["rc"]
    oid = partial_ctx["orderId"]
    it = partial_ctx["targetItem"]
    count = partial_ctx["count"]
    submit_amount = partial_ctx["submitAmount"]
    remain_before = partial_ctx["remainBefore"]
    # 0 元夹具单（无资金流动）→ 金额类断言放宽，只钉"份数 / 订单状态"维度
    zero = partial_ctx["zeroMoney"]

    before_info = rc.refundable(oid)
    before_item = next(
        (x for x in (before_info.get("refundableItems") or [])
         if x.get("orderItemId") == it.get("orderItemId")), {})
    before_refunded = int(before_item.get("refundedCount") or 0)
    whole_before = float(before_info.get("remainRefundableAmount") or 0)

    # ---------- 1. build_items 必须按"份数 × 单价"给金额 ----------
    with allure.step(f"构造部分退入参：只退 {count} 份（剩余 {remain_before - count} 份不动）"):
        items = rc.build_items(before_info, what=WHAT,
                               counts={it.get("orderItemId"): count})
        allure.attach(json.dumps(items, ensure_ascii=False, indent=1), name="refundItems",
                      attachment_type=allure.attachment_type.JSON)
        allure.attach(
            f"份数={count}，itemOriginalAmount={submit_amount}"
            f"（= 单价 × 份数，两者必须一致，见 build_items 的金额契约）",
            name="金额与份数")
    assert len(items) == 1, f"应只提交 1 个项，实际 {len(items)}"
    assert rc.items_amount(items) == submit_amount, "部分退金额 ≠ 单价 × 份数"

    # ---------- 2. 试算确定"本次该退多少"（金额含优惠分摊，以试算为准） ----------
    with allure.step("试算：确定本次应退金额（以系统试算为准，不硬套 单价×份数）"):
        calc = rc.calculate(oid, items, what=WHAT, amount=submit_amount)
        allure.attach(json.dumps(calc, ensure_ascii=False, indent=1), name="试算结果",
                      attachment_type=allure.attachment_type.JSON)
    expect_amount = float(calc.get("actualRefundAmount") or 0)
    if zero:
        assert expect_amount == 0 and whole_before == 0, (
            f"0 元单的试算应恒为 0，实际 本次={expect_amount} 整单={whole_before}"
        )
    else:
        assert 0 < expect_amount < whole_before, (
            f"部分退试算金额 {expect_amount} 应 >0 且 <整单可退 {whole_before}"
        )

    # ---------- 3. 提交 ----------
    with allure.step(f"提交部分退申请（{expect_amount} 元 / {count} 份）"):
        refund_id = rc.submit(oid, items, what=WHAT, amount=submit_amount,
                              reason="自动化部分退验证", remark="test_b_refund_partial")
        allure.attach(str(refund_id), name="refundId")
    assert isinstance(refund_id, int), f"应返回整数 refundId，实际 {refund_id!r}"

    # ---------- 4. 回查工单 + 审批 ----------
    with allure.step("回查工单拿 taskId 并审批（approvalStatus=2 = 同意）"):
        task = rc.find_task(oid, timeout=30)
        assert task, f"提交成功(refundId={refund_id})但回查不到工单"
        task_id = task.get("taskId")
        allure.attach(json.dumps(task, ensure_ascii=False, indent=1), name="工单快照",
                      attachment_type=allure.attachment_type.JSON)
        assert task_id, f"工单没有 taskId：{task}"
        rc.approval(refund_id, task_id)

    # ---------- 5. 等成功 + 校验金额 ----------
    with allure.step("轮询工单直到成功(taskStatus=2)"):
        snap = rc.wait_task(task_id, timeout=90)
        allure.attach(json.dumps(snap, ensure_ascii=False, indent=1), name="工单终态",
                      attachment_type=allure.attachment_type.JSON)
    assert snap and snap.get("taskStatus") == TASK_SUCCESS, (
        f"工单未到成功，实际 {task_status_desc((snap or {}).get('taskStatus'))}"
    )
    assert int(snap.get("taskFailCount") or 0) == 0, f"存在失败条数：{snap.get('taskFailCount')}"
    actual = float(snap.get("actualRefundAmount") or 0)
    with allure.step("核对实退金额 = 试算金额，且严格小于整单退"):
        allure.attach(f"实退={actual} 试算={expect_amount} 整单可退={whole_before}",
                      name="资金结果")
    assert abs(actual - expect_amount) < 0.02, (
        f"实退 {actual} 与试算 {expect_amount} 不符"
    )
    if zero:
        assert actual == 0, f"0 元单实退应为 0，实际 {actual}"
    else:
        assert actual < whole_before, (
            f"实退 {actual} 不小于整单可退 {whole_before} —— 这退成了全额，不是部分退"
        )

    # ---------- 6. 关键：部分退后剩余份数保留、且仍可继续退 ----------
    # 这是"部分退"区别于"全额退"的本质。判定口径**以测试环境的行为为标准**：
    # 退 N 份 → 已退份数 +N、剩余份数 -N、剩余金额 -本次退款额，订单仍可继续退。
    # 若有环境做不到（例如退部分份数却把订单整单结清），那就是 bug，用例就该失败。
    with allure.step(f"等退款落库稳定（至少 {SETTLE_SECONDS:.0f}s）后校验剩余份数保留、剩余金额正确"):
        # ★ 必须先等终态再断言：工单成功瞬间订单可能仍在过渡态。
        after_info, settle_err = _settle_refundable(rc, oid)
        if after_info is None:
            pytest.fail(
                f"部分退后订单不可再查可退性（{settle_err}）—— 只退了 {count} 份，"
                f"剩余 {remain_before - count} 份应当仍可退。"
                f"这是**已等待 {SETTLE_SECONDS:.0f}s 落库稳定后**的终态，不是过渡态："
                "说明这次部分退把整单结清了（订单已进终态），剩余份数无法再退"
            )
        after_item = next(
            (x for x in (after_info.get("refundableItems") or [])
             if x.get("orderItemId") == it.get("orderItemId")), None)
        order_after = rc.get_order(oid) or {}
        allure.attach(json.dumps({
            "refundedCount": f"{before_refunded} -> {(after_item or {}).get('refundedCount')}",
            "remainCount": f"{remain_before} -> {(after_item or {}).get('remainCount')}",
            "remainRefundableAmount": f"{before_info.get('remainRefundableAmount')} -> "
                                      f"{(after_info or {}).get('remainRefundableAmount')}",
            "订单状态": f"{order_after.get('status')}({order_after.get('statusName')})",
        }, ensure_ascii=False, indent=1), name="前后对比")

    assert after_item, (
        "部分退后可退项消失了 —— 只退了一部分份数，剩余份数应当保留且可继续退"
    )
    assert int(after_item.get("refundedCount") or 0) == before_refunded + count, (
        f"已退份数应 {before_refunded} -> {before_refunded + count}，"
        f"实际 {after_item.get('refundedCount')}"
    )
    assert int(after_item.get("remainCount") or 0) == remain_before - count, (
        f"剩余份数应 {remain_before} -> {remain_before - count}，"
        f"实际 {after_item.get('remainCount')}"
    )
    remain_amount = float(after_info.get("remainRefundableAmount") or 0)
    delta = float(before_info.get("remainRefundableAmount") or 0) - remain_amount
    assert abs(delta - actual) < 0.02, (
        f"剩余可退金额应减少约 {actual}（本次实退），实际减少 {delta}"
    )

    with allure.step("反向确认：部分退 ≠ 全额退，订单仍能继续退"):
        assert int(after_item.get("remainCount") or 0) > 0, (
            "剩余份数为 0 —— 这变成了全额退，部分退语义未成立"
        )
        if zero:
            assert remain_amount == 0, f"0 元单剩余可退额应为 0，实际 {remain_amount}"
        else:
            assert remain_amount > 0, (
                "剩余可退金额为 0 —— 钱已退光，部分退语义未成立"
            )
        again = rc.build_items(after_info, what=WHAT)
        allure.attach(json.dumps(again, ensure_ascii=False, indent=1),
                      name="剩余可退项（证明还能再退）")
    assert again, "剩余份数 > 0 却构造不出可退项"

    # 落一份结果
    artifacts = os.path.join(os.path.dirname(__file__), "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    result = {
        "env": env.name, "orderId": oid, "refundId": refund_id, "taskId": task_id,
        "partial": True, "refundCount": count, "unitPrice": partial_ctx["unitPrice"],
        "actualRefundAmount": actual,
        "refundedCount": f"{before_refunded} -> {after_item.get('refundedCount')}",
        "remainCount": f"{remain_before} -> {after_item.get('remainCount')}",
        "remainRefundableAmount": f"{before_info.get('remainRefundableAmount')} -> "
                                  f"{after_info.get('remainRefundableAmount')}",
        "orderStatusAfter": order_after.get("status"),
        "orderStatusNameAfter": order_after.get("statusName"),
    }
    with open(os.path.join(artifacts, f"{env.name}_last_partial_refund.json"), "w",
              encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    allure.attach(json.dumps(result, ensure_ascii=False, indent=1), name="本轮结果",
                  attachment_type=allure.attachment_type.JSON)
