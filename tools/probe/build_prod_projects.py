# -*- coding: utf-8 -*-
"""在生产 888 店 1:1 复刻测试店的 yb 测试项目（P02~P11，P01 已建为 127）。

用法（生产写操作，必须显式指定环境）：
    MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/build_prod_projects.py 222947 222949 ...
    MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/build_prod_projects.py --all

幂等：tools/probe/_prod_build_registry.json 记录 src→new 映射，重跑自动跳过已完成步骤。
店级差异（自动适配 888 店）：
    venue→71917、poster→888 店海报、groupIds/tagIds→[]、channelId→1
    纸质票：postageTemplateId/selfPickUpAddressId 用 888 店新建的同名配置
座位图（supportSelectSeat=true）：从测试店克隆座位图，按「场次名+票档名」重绑新 skuId。
年龄限购：逐 SKU 从测试店拉配置，按新 id 重放。
多次核销 maxVerifyCount：生产无此功能，忽略（场次照建同名）。
"""
import json
import sys
import time
import uuid
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

CST = timezone(timedelta(hours=8))
HERE = Path(__file__).parent
REG_PATH = HERE / "_prod_build_registry.json"
SEAT_XLSX = "/Users/yubing/Documents/haomai/defaultSeatTemplate (2)_副本.xlsx"

# 888 店固定差异
PROD_VENUE = 71917
PROD_POSTER = "https://ticket-img.miguvideo.com/poster/888/72a981916eb740f59a4ff0bab7e71aca.jpg"
SEAT_PROJECTS = {222959, 222961, 222963, 222965}


def ms2str(ms):
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")


def load_reg():
    if REG_PATH.exists():
        return json.load(open(REG_PATH))
    return {"projects": {}, "postage": {}, "pickup": None, "seat_xlsx_url": None}


def save_reg(reg):
    json.dump(reg, open(REG_PATH, "w"), ensure_ascii=False, indent=1)


def main():
    b = BAdminClient()
    assert b.store_id == "888", f"拒绝执行：当前不是 888 店（是 {b.store_id}）"
    tb = BAdminClient()  # 误用防护：tb 其实也是 prod（badmin 在 import 时固化环境），
    # 测试店数据全部走预导出的 _diff_test_*.json / 专用导出文件，绝不混环境。

    args = sys.argv[1:]
    if args == ["--all"]:
        src_pids = [222947, 222949, 222951, 222953, 222955, 222957, 222959, 222961, 222963, 222965]
    else:
        src_pids = [int(x) for x in args]
    reg = load_reg()

    # ---------- 测试店源数据导出（单独子进程跑，这里直接读缓存文件） ----------
    src_cache = HERE / "_src_test_data.json"
    if not src_cache.exists():
        raise SystemExit(f"缺少测试店源数据 {src_cache}，先跑 export_test_src.py")

    src = json.load(open(src_cache))

    # ---------- 0. 店级配置：运费模板 / 自提点（按测试店原样新建） ----------
    if "0.01运费yb" not in reg["postage"] or "yb运费0元" not in reg["postage"]:
        for tpl_id, tpl in src["postage_templates"].items():
            name = tpl["name"]
            body = {"storeId": int(b.store_id), "name": name,
                    "items": tpl["items"], "noDeliveryAreas": tpl.get("noDeliveryAreas", [])}
            r = b._request("POST", "/v1/shopTrade/postageTemplate",
                           params={"storeId": b.store_id}, json=body)
            new_id = r if isinstance(r, int) else (r.get("id") if isinstance(r, dict) else None)
            print(f"[店级] 运费模板「{name}」→ id={new_id}")
            reg["postage"][name] = new_id
        save_reg(reg)
    if not reg.get("pickup"):
        body = {k: src["pickup"][k] for k in
                ("name", "province", "provinceCode", "city", "cityCode",
                 "district", "districtCode", "address", "phone",
                 "getTicketTime", "instruction")}
        body["storeId"] = int(b.store_id)
        r = b._request("POST", "/v1/shopTrade/selfPickUpAddress",
                       params={"storeId": b.store_id}, json=body)
        new_id = r if isinstance(r, int) else (r.get("id") if isinstance(r, dict) else None)
        print(f"[店级] 自提点「{body['name']}」→ id={new_id}")
        reg["pickup"] = new_id
        save_reg(reg)
    print(f"[店级] 运费映射={reg['postage']} 自提点={reg['pickup']}")

    # ---------- 每个项目 ----------
    for src_pid in src_pids:
        s = src[str(src_pid)]
        rec = reg["projects"].setdefault(str(src_pid), {})
        name = s["detail"]["activityName"]
        print("=" * 74)
        print(f"### {name}（测试店 {src_pid}）")

        # 1) 项目
        if rec.get("pid"):
            pid = rec["pid"]
            print(f"[1] 已建 productId={pid}，跳过")
        else:
            body = dict(s["detail"])
            for k in ("onSale", "purchaseTimeStatus", "id"):   # 只读/计算字段
                body.pop(k, None)
            body["storeId"] = int(b.store_id)
            body["venue"] = {"poiValueId": PROD_VENUE}
            body["poster"] = PROD_POSTER
            body["goodsGroupInfo"] = {"groupIds": []}
            body["goodsTagInfo"] = {"tagIds": []}
            body["channelId"] = 1
            dt = body["deliveryInfo"]["deliveryType"]
            if 1 in dt and 3 in dt:
                body["deliveryInfo"] = {"deliveryType": dt,
                                        "postageTemplateId": reg["postage"]["yb运费0元"],
                                        "selfPickUpAddressId": reg["pickup"]}
            elif 1 in dt:
                body["deliveryInfo"] = {"deliveryType": dt,
                                        "postageTemplateId": reg["postage"]["0.01运费yb"]}
            r1 = b._request("POST", "/v1/selfProduct/add", json=body)
            pid = r1 if isinstance(r1, int) else int(str(r1).strip())
            rec["pid"] = pid
            save_reg(reg)
            print(f"[1] 新 productId={pid}")

        # 2) 场次
        def get_events(p):
            try:
                ev = b._request("GET", f"/v3/shop/product/{p}/selfBuildActivity/events",
                                params={"storeId": b.store_id})
                return ev.get("events") or ev.get("data") or []
            except Exception:
                return []     # 刚建的项目还没有场次时接口报 400，视为空
        new_ev_rows = get_events(pid)
        name2new = {e["name"]: e for e in new_ev_rows}
        todo = [e for e in s["events"] if e["name"] not in name2new]
        if todo:
            payload = {"storeId": int(b.store_id), "productId": pid,
                       "events": [{"eventName": e["name"],
                                   "startTime": ms2str(e["startTime"]),
                                   "endTime": ms2str(e["endTime"])} for e in todo]}
            b._request("POST", "/v1/selfProduct/events/add", json=payload)
            new_ev_rows = get_events(pid)
            name2new = {e["name"]: e for e in new_ev_rows}
        for e in s["events"]:
            ne = name2new[e["name"]]
            rec.setdefault("events", {})[str(e["id"])] = ne["productEventId"]
        save_reg(reg)
        print(f"[2] 场次 {len(s['events'])} 个：", {e["name"]: name2new[e["name"]]["productEventId"] for e in s["events"]})

        # 3) 票档（每场次）
        sku_map = {}   # (srcEventId, srcSkuName) -> new skuId
        for e in s["events"]:
            ne = name2new[e["name"]]
            cur = b._request("GET", "/v1/selfProduct/tickets/list",
                             params={"storeId": b.store_id, "productId": pid, "eventId": ne["productEventId"]})
            cur_names = {t["name"]: t["skuId"] for t in cur}
            for t in e["tickets"]:
                if t["name"] in cur_names:
                    sku_map[(e["id"], t["name"])] = cur_names[t["name"]]
                    continue
                addition = json.loads(t.get("skuAddition") or "{}")
                addition["bindId"] = str(uuid.uuid4())
                body = {"storeId": int(b.store_id), "productId": pid, "eventId": ne["productEventId"],
                        "name": t["name"], "salePrice": t["salePrice"],
                        "saleStatus": t.get("saleStatus", 1), "badge": t.get("badge", ""),
                        "totalStockNum": t["totalStockNum"],
                        "skuAddition": json.dumps(addition, ensure_ascii=False)}
                b._request("POST", "/v1/selfProduct/tickets/add", json=body)
                cur = b._request("GET", "/v1/selfProduct/tickets/list",
                                 params={"storeId": b.store_id, "productId": pid, "eventId": ne["productEventId"]})
                cur_names = {x["name"]: x["skuId"] for x in cur}
                sku_map[(e["id"], t["name"])] = cur_names[t["name"]]
        rec["skus"] = {f"{k[0]}|{k[1]}": v for k, v in sku_map.items()}
        save_reg(reg)
        n_sku = sum(1 for e in s["events"] for t in e["tickets"])
        print(f"[3] 票档 {n_sku} 个登记完成")

        # 4) 退款/销售配置
        if not rec.get("saleconfig"):
            sale = s["saleconfig"]
            rules = []
            for rule in sale.get("refundRuleList", []):
                relations = [{"bizType": rel["bizType"], "bizId": name2new[
                    next(e["name"] for e in s["events"] if e["id"] == rel["bizId"])]["productEventId"]}
                    for rel in rule.get("relations", [])]
                details = [{k: d[k] for k in ("startTime", "endTime", "refundMode", "refundAmount")}
                           for d in rule.get("ruleDetails", [])]
                rules.append({"relations": relations, "ruleDetails": details})
            body = {"storeId": int(b.store_id), "activityId": pid,
                    "refundSupport": sale["refundSupport"],
                    "inventoryRecoverySupport": sale["inventoryRecoverySupport"],
                    "refundTicketAllowTimes": sale["refundTicketAllowTimes"],
                    "refundRuleConfigStatus": sale["refundRuleConfigStatus"],
                    "purchaseStatus": sale["purchaseStatus"],
                    "purchaseTimeStatus": sale["purchaseTimeStatus"],
                    "tips": sale.get("tips", ""), "refundRuleList": rules}
            b._request("POST", f"/v1/shop/product/{pid}/saleConfig",
                       params={"storeId": b.store_id}, json=body)
            rec["saleconfig"] = True
            save_reg(reg)
            print("[4] 退款/销售配置已写入")

        # 6) 座位图（选座项目）—— 由 save_seat.py（前端同构 payload）单独执行，这里跳过
        if src_pid in SEAT_PROJECTS and not rec.get("seat_saved"):
            print("[6] 座位图保存交给 save_seat.py（此脚本旧 payload 会导致 500，已停用）")

        # 5) 年龄限购（逐 SKU；选座项目用座位保存后回读的新 skuId）
        if src_pid in SEAT_PROJECTS:
            for e in s["events"]:
                ne = name2new[e["name"]]
                try:
                    tiers = b._request("GET", f"/v1/seat/event/ticket/{ne['productEventId']}",
                                       params={"storeId": b.store_id, "hmProductId": pid})
                except Exception as err:
                    if "404" in str(err):
                        continue  # 生产无此接口，sku 由 save_seat.py 保存座位后登记
                    raise
                for t in (tiers.get("productSkuList") or []):
                    sku_map[(e["id"], t["priceName"])] = t["skuId"]
            rec["skus"] = {f"{k[0]}|{k[1]}": v for k, v in sku_map.items()}
            save_reg(reg)
        n_age = 0
        for e in s["events"]:
            ne = name2new[e["name"]]
            tlist = e.get("seat_tickets") or e["tickets"]
            for t in tlist:
                cfg = t.get("ageLimit")
                if not cfg:
                    continue
                if (e["id"], t["name"]) not in sku_map:
                    print(f"  ⚠️ 「{t['name']}」无新 skuId（座位未保存），跳过年龄限购")
                    continue
                new_sku = sku_map[(e["id"], t["name"])]
                body = {"storeId": int(b.store_id), "productId": pid,
                        "eventId": ne["productEventId"], "skuId": new_sku,
                        "ageLimitConfig": {"enabled": cfg["enabled"],
                                           "conditionRelation": cfg["conditionRelation"],
                                           "rules": [{k: r[k] for k in ("operator", "age", "purchaseResult")}
                                                     for r in cfg["rules"]]}}
                try:
                    b._request("POST", "/v1/selfProduct/tickets/ageLimit",
                               params={"storeId": b.store_id}, json=body)
                    n_age += 1
                except Exception as e:
                    if "404" in str(e):
                        print("  ⚠️ 生产无年龄限购接口（404），跳过该配置（与多次核销同为未上线功能）")
                        break
                    raise
        print(f"[5] 年龄限购 {n_age} 个")
    print("=" * 74)
    print("全部完成。映射表：", json.dumps({k: v.get("pid") for k, v in reg["projects"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
