# -*- coding: utf-8 -*-
"""给 yb_products.json 补上实测的层级配置：项目级字段 + 场次数 + 票档数。"""
import json, warnings
warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()
P = "/Users/yubing/Documents/haomai/.咪咕票务测试商品建设清单.ref/yb_products.json"
items = json.load(open(P))

for it in items:
    pid = it["productId"]
    try:
        d = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity",
                       params={"storeId": c.store_id}) or {}
        it["realName"] = d.get("needRealNameInfo")
        it["seatProj"] = d.get("supportSelectSeat")
        it["refund"] = d.get("refundSupport")
        it["visible"] = d.get("visible")
        it["channelId"] = d.get("channelId")
        it["city"] = (d.get("city") or {}).get("poiValueId")
    except Exception as e:
        it["realName"] = f"ERR:{str(e)[:20]}"
    try:
        ev = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                        params={"storeId": c.store_id}) or {}
        evs = ev.get("events") or []
        it["eventNum"] = len(evs)
        it["verifyCounts"] = sorted({e.get("maxVerifyCount") for e in evs})
    except Exception:
        it["eventNum"] = "?"
    try:
        tk = c._request("GET", f"/v2/shop/product/{pid}/event/ticket",
                        params={"storeId": c.store_id}) or []
        it["ticketNum"] = sum(len(e.get("ticketList") or []) for e in tk) if isinstance(tk, list) else "?"
        prices = []
        for e in (tk if isinstance(tk, list) else []):
            for t in (e.get("ticketList") or []):
                prices.append(t.get("ticketPrice"))
        it["prices"] = sorted({p for p in prices if p is not None})
    except Exception:
        it["ticketNum"] = "?"

json.dump(items, open(P, "w"), ensure_ascii=False, indent=1)

RN = {0: "非实名", 1: "一票一证", 2: "一单一证"}
print(f"{'productId':>7} | {'名称':30} | {'实名':8} | {'选座':5} | {'交付':10} | {'场次':4} | {'票档':4} | 价格档 | 库存")
print("-" * 130)
for it in items:
    rn = RN.get(it.get("realName"), it.get("realName"))
    print(f"{it['productId']:>7} | {it['name'][:28]:30} | {str(rn):8} | "
          f"{str(it.get('seatProj')):5} | {str(it.get('deliveryType')):10} | "
          f"{str(it.get('eventNum')):4} | {str(it.get('ticketNum')):4} | "
          f"{str(it.get('prices'))[:22]:22} | {it.get('stock')}")
