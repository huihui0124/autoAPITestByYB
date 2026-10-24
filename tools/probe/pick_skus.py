# -*- coding: utf-8 -*-
"""pick_skus.py —— 给清单候选商品各挑一个「最便宜且可用」的 SKU（只读）。

输出每个商品的：最低价可用 skuId、各价格档的可用 skuId 一览、场次与核销次数。

    cd ~/Documents/haomai/allure-demo
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy PYTHONPATH=. \
        ./.venv/bin/python tools/probe/pick_skus.py [pid ...]
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")

from migu_api.badmin import BAdminClient  # noqa: E402
from migu_api.client import MiguClient  # noqa: E402

DEFAULT_PIDS = [222837, 222665, 222305, 222339, 222617, 222619, 222915, 222359,
                222297, 222307, 222261, 222267]

pids = [int(x) for x in sys.argv[1:]] or DEFAULT_PIDS

c = MiguClient()
c.login()
b = BAdminClient()

report = {}
for pid in pids:
    entry = {"productId": pid}
    try:
        d = c.get_product_detail(pid)
        entry["productName"] = d.get("productName") or d.get("name")
        entry["supportSelectSeat"] = d.get("supportSelectSeat")
        entry["needRealNameInfo"] = d.get("needRealNameInfo")
    except Exception as e:  # noqa: BLE001
        entry["ERR_detail"] = str(e)[:100]

    try:
        skus = c.get_sku_list(pid)
        alive = [s for s in skus
                 if not s.get("expired") and int(s.get("count") or 0) > 0]
        alive.sort(key=lambda s: float(s.get("price") or 0))
        entry["sku_total"] = len(skus)
        entry["sku_alive"] = len(alive)
        entry["cheapest"] = (
            {"skuId": alive[0].get("skuId"), "price": alive[0].get("price"),
             "count": alive[0].get("count")}
            if alive else None
        )
        seen = {}
        for s in alive:
            p = str(s.get("price"))
            seen.setdefault(p, []).append(s.get("skuId"))
        entry["by_price"] = {k: v[:3] for k, v in sorted(seen.items(),
                                                        key=lambda kv: float(kv[0]))}
    except Exception as e:  # noqa: BLE001
        entry["ERR_sku"] = str(e)[:100]

    try:
        ev = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                        params={"storeId": b.store_id}) or {}
        entry["events"] = [
            {"productEventId": e.get("productEventId"), "name": e.get("name"),
             "maxVerifyCount": e.get("maxVerifyCount"),
             "canSaleNum": e.get("canSaleNum")}
            for e in (ev.get("events") or [])
        ]
    except Exception as e:  # noqa: BLE001
        entry["ERR_events"] = str(e)[:100]

    report[pid] = entry
    print(json.dumps(entry, ensure_ascii=False, indent=1))
    print("-" * 80)

with open("tools/probe/_picked_skus.json", "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print("saved -> tools/probe/_picked_skus.json")
