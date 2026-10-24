# -*- coding: utf-8 -*-
"""dump_full_skus.py —— 输出「场次(产品属性值) - 票档 - skuId」的完整对照（只读）。

skuCode 规律（实测确认 2026-09-29）：
    skuCode = "{场次属性值id}-{票档属性值id}"
    其中「场次属性值id」== B 端 /event/ticket 分组里的 eventId == events 里的 productEventId
         「票档属性值id」== B 端分组里 ticketList 的 ticketId
    所以可以精确把每个 SKU 归到 (场次, 票档) 上。

跑法：
    cd ~/Documents/haomai/allure-demo
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy PYTHONPATH=. \
        ./.venv/bin/python tools/probe/dump_full_skus.py 222837 222305 ...
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")

from migu_api.badmin import BAdminClient  # noqa: E402
from migu_api.client import MiguClient  # noqa: E402

pids = [int(x) for x in sys.argv[1:]]
if not pids:
    print(__doc__)
    raise SystemExit(2)

c = MiguClient()
c.login()
b = BAdminClient()

result = {}
for pid in pids:
    groups = b._request("GET", f"/v2/shop/product/{pid}/event/ticket",
                        params={"storeId": b.store_id}) or []
    events = (b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                         params={"storeId": b.store_id}) or {}).get("events") or []
    ev_meta = {e.get("productEventId"): e for e in events}

    skus = c.get_sku_list(pid)
    by_pair: dict[tuple, list] = {}
    for s in skus:
        code = s.get("skuCode") or ""
        parts = code.split("-")
        if len(parts) != 2:
            continue
        try:
            ev_id, tk_id = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        by_pair.setdefault((ev_id, tk_id), []).append(s)

    out_events = []
    for g in groups:
        ev_id = g.get("eventId")
        meta = ev_meta.get(ev_id) or {}
        tickets = []
        for t in (g.get("ticketList") or []):
            tk_id = t.get("ticketId")
            cands = by_pair.get((ev_id, tk_id)) or []
            usable = [s for s in cands
                      if not s.get("expired") and int(s.get("count") or 0) > 0]
            tickets.append({
                "ticketId": tk_id,
                "name": t.get("ticketName"),
                "price": t.get("ticketPrice"),
                "skuIds": [s.get("skuId") for s in cands],
                "skuUsable": [{"skuId": s.get("skuId"), "count": s.get("count")}
                              for s in usable],
            })
        out_events.append({
            "eventId": ev_id,
            "eventName": g.get("eventName"),
            "maxVerifyCount": meta.get("maxVerifyCount"),
            "startTime": meta.get("startTime"),
            "canSaleNum": meta.get("canSaleNum"),
            "tickets": tickets,
        })

    result[pid] = {"productId": pid, "events": out_events}

with open("tools/probe/_full_skus.json", "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

for pid, info in result.items():
    print(f"===== {pid}  ({len(info['events'])} 场次)")
    for e in info["events"]:
        tk = [f"{t['name']}({t['price']})→{t['skuUsable'] or t['skuIds']}"
              for t in e["tickets"]]
        print(f"  ev {e['eventId']} | {e['eventName']} | verify={e['maxVerifyCount']} "
              f"| canSale={e['canSaleNum']}")
        for line in tk:
            print(f"      {line}")
print("\nsaved -> tools/probe/_full_skus.json")
