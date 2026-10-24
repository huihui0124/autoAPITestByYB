# -*- coding: utf-8 -*-
"""临时探针 4：refundable 完整字段 + partialIndex 选票接口行为。用完可删。

注意：partialIndex 是 POST，可能改变后端"已选票"状态；仅在测试环境跑。
"""
import json
import os

from migu_api.badmin import BAdminError
from migu_api.refund import RefundClient

rc = RefundClient()
OID = int(os.environ.get("OID", "72880155"))
print("env:", os.environ.get("MIGU_B_ENV", "test"), "| store:", rc.store_id)

order = rc.get_order(OID) or {}
POID = order.get("purchaseOrderId")
print("订单:", json.dumps({k: order.get(k) for k in (
    "orderId", "purchaseOrderId", "status", "statusName", "productId",
    "paymentPrice", "quantity")}, ensure_ascii=False))

info = rc.refundable(OID)
print("\n=== refundable 完整返回 ===")
print(json.dumps(info, ensure_ascii=False, indent=1))

print(f"\n=== partialIndex（purchaseOrderId={POID}）===")
for idx in ([1], [0, 1], [1, 2], [0]):
    try:
        res = rc.partial_index(POID, idx)
        print(f"  indexes={idx} -> {json.dumps(res, ensure_ascii=False)}")
    except BAdminError as e:
        print(f"  indexes={idx} -> code={getattr(e,'code',None)} msg={getattr(e,'message','')}")

print("\n=== 选票后重新试算（份数 1，itemAmount=单价）===")
it = (info.get("refundableItems") or [])[0]
try:
    res = rc.calculate(OID, [{
        "orderItemId": it.get("orderItemId"), "skuId": it.get("skuId"),
        "refundTicketCount": 1, "refundMoneyCount": 1,
        "itemOriginalAmount": it.get("unitPrice"),
    }], amount=it.get("unitPrice"))
    print("  ->", json.dumps(res, ensure_ascii=False))
except BAdminError as e:
    print("  code=", getattr(e, "code", None), "msg=", getattr(e, "message", ""))
