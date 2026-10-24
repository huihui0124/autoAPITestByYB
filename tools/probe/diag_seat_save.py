# -*- coding: utf-8 -*-
"""诊断 seat/save 生产 500：用 seatmap.productSkuList 填充 ticketPriceList 重试。"""
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Users/yubing/Documents/haomai/allure-demo")
import requests
from migu_api.badmin import BAdminClient, BAdminError
from migu_api.config import b_env

HERE = Path(__file__).parent
REG_PATH = HERE / "_prod_build_registry.json"
REG = json.load(open(REG_PATH))
SRC = json.load(open(HERE / "_src_test_data.json"))
SEAT_XLSX = "/Users/yubing/Documents/haomai/defaultSeatTemplate (2)_副本.xlsx"
PROD_VENUE = 71917

src_pid = sys.argv[1] if len(sys.argv) > 1 else "222959"
ev_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0

rec = REG["projects"][src_pid]
pid = rec["pid"]
s = SRC[src_pid]
e = s["events"][ev_idx]
ne_pid = rec["events"][str(e["id"])]

b = BAdminClient()
assert b.store_id == "888", f"不是 888 店：{b.store_id}"
print(f"api={b_env.api_base} store={b.store_id} src={src_pid}->{pid} event {e['id']}->{ne_pid}「{e['name']}」")

seat_url = REG.get("seat_xlsx_url")
if not seat_url:
    up = b._request("GET", "/v1/upload/params", params={"storeId": b.store_id, "keySuffix": "xlsx"})
    r = requests.put(up["token"], data=open(SEAT_XLSX, "rb").read(), timeout=60,
                     proxies={"http": None, "https": None})
    print(f"OSS PUT -> {r.status_code}")
    assert r.status_code == 200
    seat_url = up["url"]
    REG["seat_xlsx_url"] = seat_url
    json.dump(REG, open(REG_PATH, "w"), ensure_ascii=False, indent=1)
print(f"seatTemplateUrl={seat_url}")

seat = s["seatmaps"][str(e["id"])]
area = seat["seatAreaList"][0]
new_seats = [{k: st[k] for k in ("seatName", "rowName", "floorName", "sectionName",
                                 "x", "y", "enable", "saleStatus", "color", "remarks")}
             for st in area["seatList"]]
# 票档来自 seatmap.productSkuList（e["tickets"] 对选座项目是空的！）
price_list = [{"name": sku["priceName"], "salePrice": sku["price"]}
              for sku in seat["productSkuList"]]
print(f"seats={len(new_seats)} ticketPriceList={json.dumps(price_list, ensure_ascii=False)}")

area_body = {"areaName": area["areaName"], "width": area["width"], "height": area["height"],
             "venueId": PROD_VENUE, "status": 1, "remarks": area.get("remarks", ""),
             "seatTemplateUrl": seat_url,
             "seatTemplateType": area.get("seatTemplateType", "PLANAR"),
             "seatList": new_seats}
body = {"storeId": int(b.store_id), "hmProductId": pid, "venueId": PROD_VENUE,
        "areaList": [area_body],
        "ticketPriceList": price_list,
        "eventIdList": [{"spuEventId": None, "productEventId": ne_pid, "eventName": e["name"]}]}

url = f"{b_env.api_base}/v1/seat/save?storeId={b.store_id}"
resp = b.session.post(url, json=body, timeout=60)
print(f"HTTP {resp.status_code}")
try:
    print("resp:", json.dumps(resp.json(), ensure_ascii=False)[:2000])
except ValueError:
    print("resp(raw):", resp.text[:2000])

# spuEventId 为 None 可能不合法，若失败则回读真实值再试一次
if resp.status_code != 200 or (resp.json() or {}).get("code") not in (200, None):
    ev_rows = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                         params={"storeId": b.store_id})
    rows = ev_rows.get("events") or ev_rows.get("data") or []
    target = next(x for x in rows if x["productEventId"] == ne_pid)
    body["eventIdList"][0]["spuEventId"] = target.get("spuEventId")
    print(f"重试：spuEventId={body['eventIdList'][0]['spuEventId']}")
    resp2 = b.session.post(url, json=body, timeout=60)
    print(f"HTTP {resp2.status_code}")
    try:
        print("resp:", json.dumps(resp2.json(), ensure_ascii=False)[:2000])
    except ValueError:
        print("resp(raw):", resp2.text[:2000])
