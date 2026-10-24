# -*- coding: utf-8 -*-
"""临时探针 3：部分金额 / 只退钱路径下份数是否生效。只读（只调 calculate）。用完可删。"""
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
TOTAL = round(UNIT * COUNT, 2)


def calc(label, ticket_cnt, money_cnt, item_amount, amount, has_t=1, has_m=1):
    payload = {
        "unifiedOrderId": OID, "storeId": int(rc.store_id),
        "refundType": REFUND_TYPE_PARTIAL,
        "refundItems": [{
            "orderItemId": it.get("orderItemId"), "skuId": it.get("skuId"),
            "refundTicketCount": ticket_cnt, "refundMoneyCount": money_cnt,
            "itemOriginalAmount": item_amount,
        }],
        "reason": "探针3", "remark": "probe",
        "hasTicketRefund": has_t, "hasMoneyRefund": has_m,
        "requestRefundAmount": amount,
    }
    try:
        res = rc._request("POST", "/v1/shopTrade/refund/calculate", json=payload)
        keep = {k: res.get(k) for k in ("orderOriginalAmount", "selectedItemsOriginalAmount",
                                        "recommendedRefundAmount", "actualRefundAmount")}
        print(f"[{label}] -> {json.dumps(keep, ensure_ascii=False)}")
    except BAdminError as e:
        print(f"[{label}] code={getattr(e,'code',None)} msg={getattr(e,'message','')}")


print(f"order={OID} count={COUNT} remain={REMAIN} unit={UNIT} 项总价={TOTAL}")
print("\n=== D. itemAmount=项总价，requestRefundAmount 变化 ===")
calc("reqAmt=0.01 (1份)", 1, 1, TOTAL, round(UNIT, 2))
calc("reqAmt=0.02 (全额)", REMAIN, REMAIN, TOTAL, TOTAL)
calc("reqAmt=None", 1, 1, TOTAL, None)

print("\n=== E. 只退钱(hasTicketRefund=0)，份数 1 vs 全部 ===")
calc("只退钱 退1份", 0, 1, TOTAL, round(UNIT, 2), has_t=0, has_m=1)
calc("只退钱 退全部", 0, REMAIN, TOTAL, TOTAL, has_t=0, has_m=1)

print("\n=== F. 只退票(hasMoneyRefund=0)，份数 1 vs 全部 ===")
calc("只退票 退1份", 1, 0, 0, 0, has_t=1, has_m=0)
calc("只退票 退全部", REMAIN, 0, 0, 0, has_t=1, has_m=0)

print("\n=== G. itemAmount=单价，requestRefundAmount=单价（最保守）===")
calc("itemAmt=单价 reqAmt=单价", 1, 1, UNIT, UNIT)
