# -*- coding: utf-8 -*-
"""seat/save 兜底方案：从生产老选座项目 79（身份证件入场-选座）同店克隆座位结构，
票档按模板填充色重绑为各测试项目源配置（颜色分布已验证一致：82/16/174/108/70）。

用法：MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/save_seat_from79.py <src_pid> [event_idx]
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
SEAT79 = json.load(open(HERE / "_seat_prod_79_1567.json"))

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

# 源项目（测试店）颜色 -> (priceName, price)，来自 seatmap.productSkuList + 座位 ticket
src_area = s["seatmaps"][s["events"][0]["id"]]["seatAreaList"][0]
color2ticket = {}
for st in src_area["seatList"]:
    color2ticket.setdefault(st["color"],
                            {"priceName": st["ticket"]["priceName"],
                             "price": st["ticket"]["price"]})
# 源票档列表（Xn 映射：只留 bindId）
price_list = []
for sku in s["seatmaps"][s["events"][0]["id"]].get("productSkuList") or []:
    if sku.get("status") == 0:
        continue
    addition = sku.get("skuAddition") or {}
    price_list.append({"name": sku["priceName"], "salePrice": +sku["price"],
                       "skuAddition": {"bindId": addition.get("bindId") or sku.get("bindId")}})

# 79 的一楼区域（与源单区域一致）
area79 = next(a for a in SEAT79["seatAreaList"] if a["areaName"] == "一楼")

_seat_base = int(time.time()) + int(1e5 * __import__("random").random())
_counter = [0]


def new_seat_id():
    _counter[0] += 1
    return _seat_base + _counter[0]


seats = []
for st in area79["seatList"]:
    tk = color2ticket.get(st["color"])
    if tk is None:
        raise SystemExit(f"79 座位色 {st['color']} 在源项目里找不到票档映射")
    seats.append({"seatId": new_seat_id(),
                  "sectionName": st.get("sectionName", ""),
                  "seatName": st["seatName"],
                  "rowName": st["rowName"],
                  "x": st["x"], "y": st["y"],
                  "saleStatus": 0, "color": st["color"],
                  "ticket": {"priceName": tk["priceName"], "price": tk["price"]},
                  "enable": 1, "remarks": ""})

area_body = {k: v for k, v in area79.items() if k != "areaId"}
area_body["seatList"] = seats

ok_all = True
for i, e in enumerate(s["events"]):
    if only_idx is not None and i != only_idx:
        continue
    evid = rec["events"][str(e["id"])]
    spu = row_by_peid.get(evid, {}).get("spuEventId")
    # 每个场次用新 bindId（前端复制流程对每个目标场次独立保存，bindId 随 skuAddition 走）
    pl = json.loads(json.dumps(price_list))
    for p in pl:
        p["skuAddition"]["bindId"] = p["skuAddition"]["bindId"].rsplit("-", 1)[0] + f"-{evid}"
    body = {"eventIdList": [{"spuEventId": spu, "productEventId": evid,
                             "eventName": e["name"]}],
            "hmProductId": pid, "venueId": 71917,
            "areaList": [area_body], "ticketPriceList": pl}
    resp = b.session.post(f"{b_env.api_base}/v1/seat/save?storeId={b.store_id}",
                          json=body, timeout=120)
    try:
        rj = resp.json()
    except ValueError:
        rj = {"raw": resp.text[:300]}
    ok = resp.status_code == 200 and rj.get("code") == 200
    ok_all = ok_all and ok
    print(f"「{e['name']}」{evid} 座位 {len(seats)} -> "
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
