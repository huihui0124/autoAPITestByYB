# -*- coding: utf-8 -*-
"""在测试店（storeId=1）导出 yb 项目的全部源配置 → tools/probe/_src_test_data.json

跑法（只读，不写）：
    PYTHONPATH=. ./.venv/bin/python tools/probe/export_test_src.py 222947 222949 ...
"""
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

HERE = Path(__file__).parent
OUT = HERE / "_src_test_data.json"


def main():
    b = BAdminClient()
    src_pids = [int(x) for x in sys.argv[1:]]
    out = json.load(open(OUT)) if OUT.exists() else {}

    # 店级配置（只导一次）
    if "postage_templates" not in out:
        tpl = {}
        for tid in (1561, 1601):
            d = b._request("GET", f"/v1/shopTrade/postageTemplate/{tid}", params={"storeId": 1})
            d.pop("id", None)
            d.pop("storeId", None)
            tpl[str(tid)] = d
        out["postage_templates"] = tpl
        pk = b._request("GET", "/v1/shopTrade/selfPickUpAddress/1049", params={"storeId": 1})
        out["pickup"] = pk
        print("[店级] 运费模板 ×2 + 自提点已导出")

    for pid in src_pids:
        if str(pid) in out:
            print(f"[{pid}] 已在缓存，跳过")
            continue
        detail = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity", params={"storeId": 1})
        ev = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events", params={"storeId": 1})
        ev_rows = ev.get("events") or ev.get("data") or []
        events = []
        seatmaps = {}
        for e in ev_rows:
            evid = e["productEventId"]
            tickets = b._request("GET", "/v1/selfProduct/tickets/list",
                                 params={"storeId": 1, "productId": pid, "eventId": evid})
            for t in tickets:
                # 逐票档拉年龄限购（没有配置的返回空/None）
                try:
                    al = b._request("GET", "/v1/selfProduct/tickets/ageLimit",
                                    params={"storeId": 1, "productId": pid,
                                            "eventId": evid, "skuId": t["skuId"]})
                except Exception:
                    al = None
                if isinstance(al, dict) and al.get("enabled"):
                    al.pop("id", None)
                    al.pop("storeId", None)
                    al.pop("productId", None)
                    al.pop("eventId", None)
                    al.pop("skuId", None)
                    for r in al.get("rules", []):
                        r.pop("id", None)
                    t["ageLimit"] = al
                else:
                    t["ageLimit"] = None
            events.append({"id": evid, "name": e["name"],
                           "startTime": e["startTime"], "endTime": e["endTime"],
                           "spuEventId": e.get("spuEventId"), "tickets": tickets})
            if detail.get("supportSelectSeat"):
                seat = b._request("GET", f"/v1/seat/event/{evid}",
                                  params={"storeId": 1, "hmProductId": pid})
                seatmaps[str(evid)] = seat
                # 选座项目的票档由座位图导入创建，从 seat/event/ticket 拿票档+年龄限购
                tiers = b._request("GET", f"/v1/seat/event/ticket/{evid}",
                                   params={"storeId": 1, "hmProductId": pid})
                seat_tickets = []
                for t in (tiers.get("productSkuList") or []):
                    try:
                        al = b._request("GET", "/v1/selfProduct/tickets/ageLimit",
                                        params={"storeId": 1, "productId": pid,
                                                "eventId": evid, "skuId": t["skuId"]})
                    except Exception:
                        al = None
                    entry = {"name": t["priceName"], "salePrice": t["price"],
                             "skuId": t["skuId"], "ageLimit": None}
                    if isinstance(al, dict) and al.get("enabled"):
                        for k in ("id", "storeId", "productId", "eventId", "skuId"):
                            al.pop(k, None)
                        for r in al.get("rules", []):
                            r.pop("id", None)
                        entry["ageLimit"] = al
                    seat_tickets.append(entry)
                events[-1]["seat_tickets"] = seat_tickets
        sale = b._request("GET", f"/v1/shop/product/{pid}/saleConfig", params={"storeId": 1})
        for k in ("id",):
            sale.pop(k, None)
        for r in sale.get("refundRuleList", []):
            r.pop("id", None)
            for rel in r.get("relations", []):
                rel.pop("id", None)
            for d in r.get("ruleDetails", []):
                d.pop("id", None)
        out[str(pid)] = {"detail": detail, "events": events,
                         "saleconfig": sale, "seatmaps": seatmaps}
        print(f"[{pid}] {detail['activityName']}：{len(events)} 场次 / "
              f"{sum(len(e['tickets']) for e in events)} 票档 / 座位图 {len(seatmaps)} 份")
        json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
    print("导出完成 →", OUT)


if __name__ == "__main__":
    main()
