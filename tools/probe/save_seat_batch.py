# -*- coding: utf-8 -*-
"""seat/save 批量版：一次请求保存项目的全部场次（模拟 UI 编辑器多场次勾选保存）。

用法：MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/save_seat_batch.py <src_pid>
"""
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/yubing/Documents/haomai/allure-demo")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

HERE = Path(__file__).parent
REG_PATH = HERE / "_prod_build_registry.json"
REG = json.load(open(REG_PATH))
SRC = json.load(open(HERE / "_src_test_data.json"))

src_pid = sys.argv[1]
rec = REG["projects"][src_pid]
pid = rec["pid"]
s = SRC[src_pid]

b = BAdminClient()
assert b.store_id == "888", f"不是 888 店：{b.store_id}"

ev_rows = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                     params={"storeId": b.store_id})
rows = ev_rows.get("events") or ev_rows.get("data") or []
row_by_peid = {r["productEventId"]: r for r in rows}

_seat_base = int(time.time()) + int(1e5 * __import__("random").random())
_counter = [0]


def new_seat_id():
    _counter[0] += 1
    return _seat_base + _counter[0]


def build_ticket_price_list(product_sku_list):
    out = []
    for e in product_sku_list or []:
        if e.get("status") == 0:
            continue
        addition = e.get("skuAddition") or {}
        bind_id = e.get("bindId") or addition.get("bindId")
        out.append({"name": e["priceName"], "salePrice": +e["price"],
                    "skuAddition": {k: v for k, v in
                                    (("type", addition.get("type")),
                                     ("bindId", bind_id),
                                     ("limitBuyNum", addition.get("limitBuyNum")),
                                     ("bindSkuCount", addition.get("bindSkuCount")))
                                    if v is not None}})
    return out


def build_area_list(seat_areas):
    out = []
    for area in seat_areas or []:
        a = {k: v for k, v in area.items() if k != "areaId"}
        seats = []
        for st in area.get("seatList") or []:
            seats.append({
                "seatId": new_seat_id(),
                "sectionName": st.get("sectionName", ""),
                "seatName": st["seatName"],
                "rowName": st["rowName"],
                "x": st["x"], "y": st["y"],
                "saleStatus": 0,
                "color": st["color"],
                "ticket": {"priceName": st["ticket"]["priceName"],
                           "price": st["ticket"]["price"]},
                "enable": 1,
                "remarks": "",
            })
        a["seatList"] = seats
        out.append(a)
    return out


ev_ids, names = [], []
area0 = pl0 = None
for e in s["events"]:
    evid = rec["events"][str(e["id"])]
    seat = s["seatmaps"][str(e["id"])]
    spu = row_by_peid.get(evid, {}).get("spuEventId")
    ev_ids.append({"spuEventId": spu, "productEventId": evid, "eventName": e["name"]})
    names.append(e["name"])
    area0 = build_area_list(seat.get("seatAreaList"))
    pl0 = build_ticket_price_list(seat.get("productSkuList"))

body = {"eventIdList": ev_ids, "hmProductId": pid, "venueId": 71917,
        "areaList": area0, "ticketPriceList": pl0}
resp = b.session.post(f"{b_env.api_base}/v1/seat/save?storeId={b.store_id}",
                      json=body, timeout=180)
try:
    rj = resp.json()
except ValueError:
    rj = {"raw": resp.text[:300]}
ok = resp.status_code == 200 and rj.get("code") == 200
print(f"批量保存 {len(ev_ids)} 场次{names} -> {'OK' if ok else 'FAIL'} "
      f"{json.dumps(rj, ensure_ascii=False)[:200]}")
time.sleep(2)
all_ok = ok
for e in s["events"]:
    evid = rec["events"][str(e["id"])]
    try:
        d = b._request("GET", f"/v1/seat/event/{evid}",
                       params={"storeId": b.store_id, "hmProductId": pid})
        skus = [(x.get("priceName"), x.get("skuId"), x.get("price"))
                for x in d.get("productSkuList") or []]
        n_seats = sum(len(a.get("seatList") or []) for a in d.get("seatAreaList") or [])
        print(f"   回读「{e['name']}」{evid}: {n_seats} 座, 票档 {len(skus)} 个 {skus[:2]}")
        if len(skus) != 5:
            all_ok = False
        else:
            for x in d.get("productSkuList") or []:
                rec.setdefault("skus", {})[f"{e['id']}|{x['priceName']}"] = x["skuId"]
    except Exception as err:
        all_ok = False
        print(f"   回读「{e['name']}」{evid} ERR {str(err)[:100]}")

if all_ok:
    rec["seat_saved"] = True
    json.dump(REG, open(REG_PATH, "w"), ensure_ascii=False, indent=1)
