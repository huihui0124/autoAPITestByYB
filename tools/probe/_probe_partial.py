# -*- coding: utf-8 -*-
"""临时探针：找「可部分退」的订单（多件或多份）。用完可删。

只读，不产生任何写操作。
"""
import json
import os

from migu_api.badmin import BAdminError
from migu_api.refund import RefundClient

rc = RefundClient()
print("env:", os.environ.get("MIGU_B_ENV", "test"), "| storeId:", rc.store_id)
print("B 端会话自检 totalNum =", rc.list_orders(page_size=1).get("totalNum"))

SCAN_STATUS = (11, 7, 0, 5, 14)
multi = []      # 多份（同一项 count >= 2）
multi_item = []  # 多项（refundableItems 长度 >= 2）
paid_plain = []
seen = set()

for st in SCAN_STATUS:
    page = rc.list_orders(order_status=st, page_size=50)
    rows = page.get("data") or []
    for row in rows:
        oid = row.get("orderId")
        if oid in seen:
            continue
        seen.add(oid)
        try:
            info = rc.refundable(oid)
        except BAdminError:
            continue
        items = info.get("refundableItems") or []
        if not items:
            continue
        rec = {
            "orderId": oid,
            "orderStatusName": row.get("statusName"),
            "paid": info.get("orderPaidAmount"),
            "remain": info.get("remainRefundableAmount"),
            "items": [
                {k: it.get(k) for k in ("orderItemId", "skuId", "count",
                                        "refundedCount", "remainCount",
                                        "unitPrice", "canRefundTicket")}
                for it in items
            ],
        }
        if len(items) >= 2:
            multi_item.append(rec)
        elif int(items[0].get("remainCount") or 0) >= 2:
            multi.append(rec)
        elif float(info.get("orderPaidAmount") or 0) > 0:
            paid_plain.append(rec)

print("\n=== 单一项但可退份数 >= 2（可做「部分份数退款」）===")
for r in multi[:8]:
    print(json.dumps(r, ensure_ascii=False))
print("命中:", len(multi))

print("\n=== 多可退项（可做「部分项退款」）===")
for r in multi_item[:8]:
    print(json.dumps(r, ensure_ascii=False))
print("命中:", len(multi_item))

print("\n=== 有实付金额但单项单份（仅可全额退，供对比）===")
for r in paid_plain[:5]:
    print(json.dumps(r, ensure_ascii=False))
print("命中:", len(paid_plain))
