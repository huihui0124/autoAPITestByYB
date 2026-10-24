# -*- coding: utf-8 -*-
"""临时探针：试出「部分份数退款」的正确入参。只读（只调 calculate）。用完可删。

要回答的问题：
  1. refundTicketCount / refundMoneyCount 传 1（而非 remainCount）时，calculate 认不认？
  2. requestRefundAmount 该传部分金额还是全额？
  3. 返回的 actualRefundAmount 是部分还是全额？
"""
import json
import os

from migu_api.badmin import BAdminError
from migu_api.refund import REFUND_TYPE_PARTIAL, RefundClient, WHAT_BOTH, WHAT_MONEY, WHAT_TICKET

rc = RefundClient()
OID = int(os.environ.get("OID", "72880155"))
print("env:", os.environ.get("MIGU_B_ENV", "test"), "| storeId:", rc.store_id, "| order:", OID)

info = rc.refundable(OID)
it = (info.get("refundableItems") or [])[0]
print("可退性:", json.dumps({k: info.get(k) for k in (
    "orderOriginalAmount", "orderPaidAmount", "totalRefundedAmount",
    "remainRefundableAmount")}, ensure_ascii=False))
print("明细项:", json.dumps(it, ensure_ascii=False))

REMAIN = int(it.get("remainCount") or 0)
UNIT = float(it.get("unitPrice") or 0)
print(f"remainCount={REMAIN} unitPrice={UNIT} remainAmount={info.get('remainRefundableAmount')}")


def make_item(ticket_cnt, money_cnt, item_id, sku):
    return {
        "orderItemId": item_id,
        "skuId": sku,
        "refundTicketCount": ticket_cnt,
        "refundMoneyCount": money_cnt,
        "itemOriginalAmount": UNIT,
    }


def calc(label, ticket_cnt, money_cnt, amount, what=WHAT_BOTH):
    payload = {
        "unifiedOrderId": OID,
        "storeId": int(rc.store_id),
        "refundType": REFUND_TYPE_PARTIAL,
        "refundItems": [make_item(ticket_cnt, money_cnt, it.get("orderItemId"), it.get("skuId"))],
        "reason": "部分退探针",
        "remark": "probe",
        "hasTicketRefund": 1 if what in (WHAT_TICKET, WHAT_BOTH) else 0,
        "hasMoneyRefund": 1 if what in (WHAT_MONEY, WHAT_BOTH) else 0,
        "requestRefundAmount": amount,
    }
    try:
        res = rc._request("POST", "/v1/shopTrade/refund/calculate", json=payload)
        print(f"[{label}] OK -> {json.dumps(res, ensure_ascii=False)}")
    except BAdminError as e:
        print(f"[{label}] code={getattr(e,'code',None)} msg={getattr(e,'message','')}")


print("\n=== 试算矩阵（退 1 份 vs 全部）===")
calc("退1份 / 金额传1份", 1, 1, round(UNIT, 2))
calc("退1份 / 金额传全额", 1, 1, info.get("remainRefundableAmount"))
calc("退1份 / 金额不传(None)", 1, 1, None)
calc("退全部 / 金额传全额", REMAIN, REMAIN, info.get("remainRefundableAmount"))
calc("只退票(1份) / 金额0", 1, 0, 0, what=WHAT_TICKET)
calc("只退钱(1份) / 金额1份", 0, 1, round(UNIT, 2), what=WHAT_MONEY)
if REMAIN >= 2:
    calc("退2份 / 金额传2份", 2, 2, round(UNIT * 2, 2))
