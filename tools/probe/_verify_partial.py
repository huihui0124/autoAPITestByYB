# -*- coding: utf-8 -*-
"""临时验证：修好的 build_items 是否正确支持部分退（按份数）。用完可删。

步骤：只读自检 itemOriginalAmount 金额 → 实跑部分退 → 前后快照对比。
"""
import json
import os

from migu_api.refund import RefundClient, what_desc

rc = RefundClient()
OID = int(os.environ.get("OID", "72866928"))
N = int(os.environ.get("N", "2"))
APPLY = os.environ.get("APPLY") == "1"


def snap(oid):
    info = rc.refundable(oid)
    it = info.get("refundableItems") or [{}]
    return info, it


info, items_raw = snap(OID)
it = items_raw[0]
print("env:", os.environ.get("MIGU_B_ENV", "test"), "| order:", OID)
print("退款前:", json.dumps({
    "paid": info.get("orderPaidAmount"), "refunded": info.get("totalRefundedAmount"),
    "remain": info.get("remainRefundableAmount"),
    "count": it.get("count"), "refundedCount": it.get("refundedCount"),
    "remainCount": it.get("remainCount"), "unitPrice": it.get("unitPrice"),
}, ensure_ascii=False))

iid = it.get("orderItemId")
counts = {iid: N}
built = rc.build_items(info, counts=counts)
print(f"\n本次只退 {N} 份 -> refundItems: {json.dumps(built, ensure_ascii=False)}")
print("金额合计 =", rc.items_amount(built),
      "| 预期 =", round(float(it.get("unitPrice")) * N, 2))
print("整单可退总额 =", info.get("remainRefundableAmount"), "（用于判定是否真部分退）")

assert abs(rc.items_amount(built) - float(it.get("unitPrice")) * N) < 1e-6, "金额算错"

# 对照：不传 counts 时应退满
full = rc.build_items(info)
print("不传 counts（退满）金额 =", rc.items_amount(full))
assert abs(rc.items_amount(full) - float(info.get("remainRefundableAmount"))) < 1e-6, \
    "退满金额应等于整单可退额（这是本次修复的缺陷点）"

if not APPLY:
    print("\n[只读自检通过] 加 APPLY=1 才真跑")
    raise SystemExit(0)

print("\n=== 实跑部分退 ===")
res = rc.refund_order(OID, counts=counts, reason="自动化部分退验证", task_timeout=60)
print("summary:", json.dumps({k: res.get(k) for k in (
    "amount", "fullAmount", "partial", "refundId", "taskId", "approved")},
    ensure_ascii=False))
task = res.get("task") or {}
print("工单终态:", json.dumps({k: task.get(k) for k in (
    "taskStatus", "statusDesc", "taskSuccessCount", "actualRefundAmount",
    "hasTicketRefund", "hasMoneyRefund")}, ensure_ascii=False))

after_info, after_items = snap(OID)
ait = after_items[0]
print("退款后:", json.dumps({
    "paid": after_info.get("orderPaidAmount"), "refunded": after_info.get("totalRefundedAmount"),
    "remain": after_info.get("remainRefundableAmount"),
    "refundedCount": ait.get("refundedCount"), "remainCount": ait.get("remainCount"),
}, ensure_ascii=False))
print(f">>> refundedCount {it.get('refundedCount')} -> {ait.get('refundedCount')} "
      f"(应 +{N}) | remain 金额 {info.get('remainRefundableAmount')} -> "
      f"{after_info.get('remainRefundableAmount')} (应 -{res.get('amount')})")
