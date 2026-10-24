# -*- coding: utf-8 -*-
"""临时探针 2：验证 itemOriginalAmount 的语义（单价 vs 项总金额）。只读。用完可删。"""
import json
import os

from migu_api.badmin import BAdminError
from migu_api.refund import REFUND_TYPE_PARTIAL, RefundClient

rc = RefundClient()
OID = int(os.environ.get("OID", "72880155"))
info = rc.refundable(OID)
it = (info.get("refundableItems") or [])[0]
REMAIN = int(it.get("remainCount") or 0)
UNIT = float(it.get("unitPrice") or 0)
COUNT = int(it.get("count") or 0)
print(f"order={OID} count={COUNT} remainCount={REMAIN} unitPrice={UNIT} "
      f"项总价={round(UNIT * COUNT, 2)} remainAmount={info.get('remainRefundableAmount')}")


def calc(label, ticket_cnt, money_cnt, item_amount, amount=None):
    payload = {
        "unifiedOrderId": OID,
        "storeId": int(rc.store_id),
        "refundType": REFUND_TYPE_PARTIAL,
        "refundItems": [{
            "orderItemId": it.get("orderItemId"),
            "skuId": it.get("skuId"),
            "refundTicketCount": ticket_cnt,
            "refundMoneyCount": money_cnt,
            "itemOriginalAmount": item_amount,
        }],
        "reason": "部分退探针2",
        "remark": "probe",
        "hasTicketRefund": 1,
        "hasMoneyRefund": 1,
        "requestRefundAmount": amount,
    }
    try:
        res = rc._request("POST", "/v1/shopTrade/refund/calculate", json=payload)
        keep = {k: res.get(k) for k in ("orderOriginalAmount", "orderPaidAmount",
                                        "selectedItemsOriginalAmount",
                                        "recommendedRefundAmount", "actualRefundAmount")}
        print(f"[{label}] -> {json.dumps(keep, ensure_ascii=False)}")
    except BAdminError as e:
        print(f"[{label}] code={getattr(e,'code',None)} msg={getattr(e,'message','')}")


TOTAL = round(UNIT * COUNT, 2)
print("\n=== A. itemOriginalAmount 传「项总金额」，份数 1 vs 全部 ===")
calc("itemAmt=项总价 退1份", 1, 1, TOTAL)
calc("itemAmt=项总价 退全部", REMAIN, REMAIN, TOTAL)

print("\n=== B. itemOriginalAmount 传「单价」（当前实现的做法）===")
calc("itemAmt=单价 退1份", 1, 1, UNIT)
calc("itemAmt=单价 退全部", REMAIN, REMAIN, UNIT)

print(f"\n=== C. 订单 purchaseOrderId = {json.dumps({k: (rc.get_order(OID) or {}).get(k) for k in ('purchaseOrderId','orderId','status','statusName','productId')}, ensure_ascii=False)} ===")
