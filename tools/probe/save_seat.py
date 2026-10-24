# -*- coding: utf-8 -*-
"""seat/save 修复版 —— 精确复刻 B 端前端「复制座位图」流程（逆向自 p__store___layout.js）。

关键规则（与前端 Qn/Xn/g 函数一致）：
  - areaList：源区域去掉 areaId，其余字段原样（含 eventId/venueId/width/height/status/remarks）
  - seatList：{seatId:全新唯一id, sectionName, seatName, rowName, x, y,
              saleStatus:0(NOT_SOLD), color, ticket:{priceName,price}, enable:1, remarks:""}
              不带 skuId
  - ticketPriceList：源 productSkuList -> {name: priceName, salePrice: +price,
              skuAddition:{type?, bindId, limitBuyNum?, bindSkuCount?}}（过滤 status==0）
  - seatTemplateUrl：用源的 sspw-hdsz2 CDN 域名（ticket-img.miguvideo.com 后端拉不到 -> 保存异常）
  - body 不含 storeId（storeId 走 query）

用法：MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/save_seat.py <src_pid> [event_idx]
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

src_pid = sys.argv[1] if len(sys.argv) > 1 else "222959"
only_idx = int(sys.argv[2]) if len(sys.argv) > 2 else None

rec = REG["projects"][src_pid]
pid = rec["pid"]
s = SRC[src_pid]

b = BAdminClient()
assert b.store_id == "888", f"不是 888 店：{b.store_id}"

ev_rows = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                     params={"storeId": b.store_id})
rows = ev_rows.get("events") or ev_rows.get("data") or []
row_by_peid = {r["productEventId"]: r for r in rows}

# 前端 Jn 的 seatId 生成器：epoch 秒 + 随机 1e5，之后自增
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
                "saleStatus": 0,             # NOT_SOLD
                "color": st["color"],
                "ticket": {"priceName": st["ticket"]["priceName"],
                           "price": st["ticket"]["price"]},
                "enable": 1,
                "remarks": "",
            })
        a["seatList"] = seats
        out.append(a)
    return out


ok_all = True
for i, e in enumerate(s["events"]):
    if only_idx is not None and i != only_idx:
        continue
    evid = rec["events"][str(e["id"])]
    seat = s["seatmaps"][str(e["id"])]
    spu = row_by_peid.get(evid, {}).get("spuEventId")
    body = {"eventIdList": [{"spuEventId": spu, "productEventId": evid,
                             "eventName": e["name"]}],
            "hmProductId": pid,
            "venueId": 71917,
            "areaList": build_area_list(seat.get("seatAreaList")),
            "ticketPriceList": build_ticket_price_list(seat.get("productSkuList"))}
    resp = b.session.post(f"{b_env.api_base}/v1/seat/save?storeId={b.store_id}",
                          json=body, timeout=120)
    try:
        rj = resp.json()
    except ValueError:
        rj = {"raw": resp.text[:300]}
    ok = resp.status_code == 200 and rj.get("code") == 200
    ok_all = ok_all and ok
    print(f"「{e['name']}」{evid} 座位 {sum(len(a['seatList']) for a in body['areaList'])} -> "
          f"{'OK' if ok else 'FAIL'} {json.dumps(rj, ensure_ascii=False)[:200]}")
    if ok:
        d = b._request("GET", f"/v1/seat/event/{evid}",
                       params={"storeId": b.store_id, "hmProductId": pid})
        json.dump(d, open(HERE / f"_seat_prod_{evid}.json", "w"), ensure_ascii=False, indent=1)
        skus = [(x.get("priceName"), x.get("skuId"), x.get("price"))
                for x in d.get("productSkuList") or []]
        n_seats = sum(len(a.get("seatList") or []) for a in d.get("seatAreaList") or [])
        print(f"   回读: {n_seats} 座, 票档 {skus}")

if ok_all:
    rec["seat_saved"] = True
    json.dump(REG, open(REG_PATH, "w"), ensure_ascii=False, indent=1)
