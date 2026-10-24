# -*- coding: utf-8 -*-
"""dump_target_events.py —— 为「测试专用项目清单」取实测的 eventId / skuId。

只读：拉目标商品的项目级字段 + 场次列表 + 每场次票档，打印成可直接抄进
catalog.json 的结构。跑法（绕开本地代理）：

    cd ~/Documents/haomai/allure-demo
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
        ./.venv/bin/python tools/probe/dump_target_events.py [pid ...]
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")

from migu_api.badmin import BAdminClient  # noqa: E402

# 清单 v4 里「可复用现有商品」那批
DEFAULT_PIDS = [222837, 222305, 222339, 222617, 222619, 222915, 222359, 222665]

pids = [int(x) for x in sys.argv[1:]] or DEFAULT_PIDS
c = BAdminClient()

out = []
for pid in pids:
    item = {"productId": pid}
    try:
        d = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity",
                       params={"storeId": c.store_id}) or {}
        item["productName"] = d.get("productName") or d.get("name")
        item["supportSelectSeat"] = d.get("supportSelectSeat")
        item["needRealNameInfo"] = d.get("needRealNameInfo")
        di = d.get("deliveryInfo") or {}
        item["deliveryType"] = di.get("deliveryType")
        item["visible"] = d.get("visible")
    except Exception as e:  # noqa: BLE001
        item["ERR_product"] = str(e)[:120]

    try:
        ev = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                        params={"storeId": c.store_id}) or {}
        item["events_raw"] = [
            {"eventId": e.get("eventId") or e.get("id"),
             "eventName": e.get("eventName") or e.get("name"),
             "maxVerifyCount": e.get("maxVerifyCount"),
             "startTime": e.get("startTime"),
             "totalStockNum": e.get("totalStockNum")}
            for e in (ev.get("events") or [])
        ]
    except Exception as e:  # noqa: BLE001
        item["ERR_events"] = str(e)[:120]

    try:
        tk = c._request("GET", f"/v2/shop/product/{pid}/event/ticket",
                        params={"storeId": c.store_id}) or []
        item["tickets_raw"] = [
            {"eventId": e.get("eventId") or e.get("id"),
             "tickets": [{"skuId": t.get("skuId"),
                          "ticketName": t.get("ticketName") or t.get("skuName"),
                          "ticketPrice": t.get("ticketPrice"),
                          "saleStatus": t.get("saleStatus"),
                          "count": t.get("count") or t.get("totalStockNum")}
                         for t in (e.get("ticketList") or [])]}
            for e in (tk if isinstance(tk, list) else [])
        ]
    except Exception as e:  # noqa: BLE001
        item["ERR_tickets"] = str(e)[:120]

    out.append(item)
    print(json.dumps(item, ensure_ascii=False, indent=1))
    print("-" * 90)
