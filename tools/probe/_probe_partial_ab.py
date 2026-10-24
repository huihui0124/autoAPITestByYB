# -*- coding: utf-8 -*-
"""临时实验：确定「部分退」的控制点。仅测试环境。用完可删。

实验 A（OID_A，2 份 × 0.01）：itemOriginalAmount 传 1 份的钱(0.01)，份数 1
    → 若退款后 refundedCount=1/remainCount=1/remain=0.01 ⇒ 按金额部分退可行
实验 B（OID_B，3 份 × 0.01）：itemOriginalAmount 传项总价(0.03)，份数 1
    → 看实际退 0.03 还是 0.01（判定份数是否参与）
"""
import json
import os

from migu_api.badmin import BAdminError
from migu_api.refund import RefundClient, task_status_desc

rc = RefundClient()


def snapshot(oid):
    info = rc.refundable(oid)
    it = (info.get("refundableItems") or [{}])[0]
    return {
        "paid": info.get("orderPaidAmount"),
        "refunded": info.get("totalRefundedAmount"),
        "remain": info.get("remainRefundableAmount"),
        "count": it.get("count"), "refundedCount": it.get("refundedCount"),
        "remainCount": it.get("remainCount"), "unitPrice": it.get("unitPrice"),
    }


def run(label, oid, ticket_cnt, item_amount, req_amount):
    print("=" * 70)
    print(f"### {label}  order={oid} 份数={ticket_cnt} itemAmount={item_amount} reqAmt={req_amount}")
    before = snapshot(oid)
    print("  退款前:", json.dumps(before, ensure_ascii=False))

    info = rc.refundable(oid)
    it = (info.get("refundableItems") or [])[0]
    items = [{
        "orderItemId": it.get("orderItemId"), "skuId": it.get("skuId"),
        "refundTicketCount": ticket_cnt, "refundMoneyCount": ticket_cnt,
        "itemOriginalAmount": item_amount,
    }]
    calc = rc.calculate(oid, items, amount=req_amount)
    print("  试算:", json.dumps({k: calc.get(k) for k in (
        "selectedItemsOriginalAmount", "recommendedRefundAmount", "actualRefundAmount")},
        ensure_ascii=False))

    try:
        rid = rc.submit(oid, items, amount=req_amount, reason=f"部分退实验-{label}",
                        remark="partial-probe")
        print("  提交 -> refundId =", rid)
    except BAdminError as e:
        print("  提交失败:", getattr(e, "code", None), getattr(e, "message", ""))
        return

    task = rc.find_task(oid, timeout=25)
    if not task:
        print("  !! 回查不到工单")
        return
    print("  工单:", json.dumps({k: task.get(k) for k in (
        "taskId", "taskStatus", "taskTotalCount", "refundTypeDesc",
        "actualRefundAmount", "hasTicketRefund", "hasMoneyRefund")}, ensure_ascii=False))
    rc.approval(rid, task["taskId"])
    snap = rc.wait_task(task["taskId"], timeout=60)
    print("  终态:", json.dumps({k: snap.get(k) for k in (
        "taskStatus", "statusDesc", "taskTotalCount", "taskSuccessCount",
        "taskFailCount", "actualRefundAmount")}, ensure_ascii=False),
        task_status_desc(snap.get("taskStatus")))
    after = snapshot(oid)
    print("  退款后:", json.dumps(after, ensure_ascii=False))
    print(f"  >>> 判定: 实退={snap.get('actualRefundAmount')} "
          f"refundedCount {before['refundedCount']}->{after['refundedCount']} "
          f"remain {before['remain']}->{after['remain']}")


A = int(os.environ.get("OID_A", "72880155"))
B = int(os.environ.get("OID_B", "72880121"))
if os.environ.get("ONLY") != "B":
    run("A: itemAmount=1份的钱", A, 1, 0.01, 0.01)
if os.environ.get("ONLY") != "A":
    run("B: itemAmount=项总价, 份数1", B, 1, 0.03, 0.03)
