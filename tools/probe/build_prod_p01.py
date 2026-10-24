# -*- coding: utf-8 -*-
"""在生产 888 店 1:1 复刻测试店 P01（222945）。

用法（生产写操作，必须显式指定环境）：
    MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/build_prod_p01.py

复刻范围：项目级全字段 + 3 场次 + 每场次 5 票档（含未开售 fixedSaleTime /
限购 limitBuyNum / 售罄库存0）+ 条件退退款规则。
差异项（店级字典，无法照抄，已按 888 店既有商品对齐）：
    venue poiValueId: 115667(测试店) → 71917(888店)
    poster:           /poster/1/…   → 888 店海报（借 109 的）
    goodsGroupInfo / goodsTagInfo: [2115]/[23] → []（888 店未建同款分组/标签）
    channelId:        19            → 1
多次核销 maxVerifyCount 按用户要求忽略（生产暂无该功能），场次照建同名。
"""
import json
import sys
import uuid
import warnings
from datetime import datetime, timezone, timedelta

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

CST = timezone(timedelta(hours=8))
SRC_PID = 222945


def ms2str(ms):
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM:SS'（Event.startTime 是字符串类型）。"""
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")


def main():
    b = BAdminClient()
    assert b.store_id == "888", f"拒绝执行：当前不是 888 店（是 {b.store_id}）"

    # argv[1] 传已建好的 productId → 跳过第 1 步（防重复建项目）
    existing_pid = int(sys.argv[1]) if len(sys.argv) > 1 else None

    src_detail = json.load(open("tools/probe/_p01_detail.json"))
    src_events = json.load(open("tools/probe/_p01_events_edit.json"))
    src_tickets = json.load(open("tools/probe/_p01_tickets_all.json"))
    # 源 saleConfig 由测试环境导出（见 _p01_saleconfig.json），生产客户端拉不到测试店
    src_sale = json.load(open("tools/probe/_p01_saleconfig.json"))

    ev_rows = src_events.get("data") or src_events
    if isinstance(ev_rows, dict):
        ev_rows = ev_rows.get("data", [])

    # ---------- 1. 项目 ----------
    project_body = {
        "storeId": int(b.store_id),
        "activityName": src_detail["activityName"],
        "categoryId": src_detail["categoryId"],
        "superCategoryId": src_detail["superCategoryId"],
        "city": {"poiValueId": 68971},                 # 两店一致（南京）
        "venue": {"poiValueId": 71917},                # ★ 888 店场地（测试店是 115667）
        "displayType": src_detail["displayType"],
        "poster": "https://ticket-img.miguvideo.com/poster/888/72a981916eb740f59a4ff0bab7e71aca.jpg",  # ★ 888 店海报
        "activityDesc": src_detail.get("activityDesc", ""),
        "deliveryInfo": src_detail["deliveryInfo"],    # {"deliveryType":[4],"deliveryOrigin":"HAOMAI"}
        "goodsGroupInfo": {"groupIds": []},            # ★ 888 店未建同款分组
        "goodsTagInfo": {"tagIds": []},                # ★ 888 店未建同款标签
        "recommendWords": src_detail.get("recommendWords", ""),
        "activityDuration": src_detail.get("activityDuration", ""),
        "ticketTips": src_detail.get("ticketTips", ""),
        "purchaseTips": src_detail.get("purchaseTips", ""),
        "visible": src_detail["visible"],
        "supportSelectSeat": src_detail["supportSelectSeat"],
        "popTipsSwitch": src_detail.get("popTipsSwitch", False),
        "popTipsTitle": src_detail.get("popTipsTitle", ""),
        "popTipsContent": src_detail.get("popTipsContent", ""),
        "needRealNameInfo": src_detail["needRealNameInfo"],
        "thirdVerificationType": src_detail["thirdVerificationType"],
        "canConsumeAfterEndTime": src_detail["canConsumeAfterEndTime"],
        "channelId": 1,                                # ★ 888 店渠道（测试店是 19）
        "channelType": src_detail["channelType"],
    }
    print("=" * 70)
    print("[1/4] POST /v1/selfProduct/add")
    print(json.dumps(project_body, ensure_ascii=False)[:300], "…")
    if existing_pid:
        pid = existing_pid
        print(f"跳过创建，使用已存在的 productId = {pid}")
    else:
        r1 = b._request("POST", "/v1/selfProduct/add", json=project_body)
        print("响应:", json.dumps(r1, ensure_ascii=False)[:400])
        # 响应可能是裸数字（新 productId）、dict 或 str
        if isinstance(r1, (int, float)):
            pid = int(r1)
        elif isinstance(r1, str) and r1.strip().isdigit():
            pid = int(r1.strip())
        else:
            pid = (r1.get("productId") or r1.get("id") if isinstance(r1, dict) else None) \
                  or (r1.get("data", {}).get("productId") if isinstance(r1, dict) and isinstance(r1.get("data"), dict) else None)
        if not pid:
            raise SystemExit("✗ 没拿到新 productId，停。")
        print(f"★ 新 productId = {pid}")

    # ---------- 2. 场次 ----------
    print("=" * 70)
    print("[2/4] POST /v1/selfProduct/events/add")
    # 幂等：先回读现有场次，按名字跳过已建的
    cur = b._request("GET", "/v1/selfProduct/events/list",
                     params={"storeId": b.store_id, "productId": pid})
    cur_rows = cur.get("data") or cur
    if isinstance(cur_rows, dict):
        cur_rows = cur_rows.get("data", [])
    cur_names = {e["eventName"] for e in cur_rows}
    todo = [e for e in ev_rows if e["eventName"] not in cur_names]
    print(f"  已存在 {len(cur_names)} 个场次，待创建 {len(todo)} 个")
    events_payload = {
        "storeId": int(b.store_id),
        "productId": pid,
        "events": [
            {"eventName": e["eventName"],
             "startTime": ms2str(e["startTime"]),
             "endTime": ms2str(e["endTime"])}
            for e in todo
        ],
    }
    print(json.dumps(events_payload, ensure_ascii=False))
    if todo:
        r2 = b._request("POST", "/v1/selfProduct/events/add", json=events_payload)
        print("响应:", json.dumps(r2, ensure_ascii=False)[:400])
    else:
        print("  全部已建，跳过")
    # 建完回读拿新 eventId
    new_events = b._request("GET", "/v1/selfProduct/events/list",
                            params={"storeId": b.store_id, "productId": pid})
    new_ev_rows = new_events.get("data") or new_events
    if isinstance(new_ev_rows, dict):
        new_ev_rows = new_ev_rows.get("data", [])
    print("新场次:", [(e["id"], e["eventName"]) for e in new_ev_rows])
    name2event = {e["eventName"]: e["id"] for e in new_ev_rows}

    # ---------- 3. 票档 ----------
    print("=" * 70)
    print("[3/4] POST /v1/selfProduct/tickets/add")
    # 现有票档（场次→名字集合）
    cur_tk = b._request("GET", "/v2/shop/product/{0}/event/ticket".format(pid),
                        params={"storeId": b.store_id}) or []
    have = {}
    for g in cur_tk:
        have[g.get("eventId")] = {t.get("ticketName") for t in (g.get("ticketList") or [])}
    for e in ev_rows:                                   # 源场次（保持同名对应）
        ev_id = name2event[e["eventName"]]
        if t_names := have.get(ev_id):
            pass
        else:
            t_names = set()
        for t in src_tickets[str(e["id"])]:
            if t["name"] in t_names:
                print(f"  [{e['eventName']} / {t['name']}] 已存在，跳过")
                continue
            addition = json.loads(t.get("skuAddition") or "{}")
            addition["bindId"] = str(uuid.uuid4())      # bindId 每票档新生成
            body = {
                "storeId": int(b.store_id),
                "productId": pid,
                "eventId": ev_id,
                "name": t["name"],
                "salePrice": t["salePrice"],
                "saleStatus": t.get("saleStatus", 1),
                "badge": t.get("badge", ""),
                "totalStockNum": t["totalStockNum"],
                "skuAddition": json.dumps(addition, ensure_ascii=False),
            }
            r3 = b._request("POST", "/v1/selfProduct/tickets/add", json=body)
            print(f"  [{e['eventName']} / {t['name']}] →", json.dumps(r3, ensure_ascii=False)[:160])

    # ---------- 4. 退款规则（只有「条件退场次」绑 FREE 规则，照抄） ----------
    print("=" * 70)
    print("[4/4] POST /v1/shop/product/{pid}/saleConfig")
    # 找源 saleConfig 里绑定的源 eventId（7904071 = 条件退场次）
    bound_src = {rel["bizId"] for r in src_sale.get("refundRuleList", [])
                 for rel in r.get("relations", [])}
    refund_rules = []
    for rule in src_sale.get("refundRuleList", []):
        relations = [{"bizType": rel["bizType"],
                      "bizId": name2event[next(e["eventName"] for e in ev_rows
                                               if e["id"] == rel["bizId"])]}
                     for rel in rule.get("relations", [])]
        rule_details = [{"startTime": d["startTime"], "endTime": d["endTime"],
                         "refundMode": d["refundMode"], "refundAmount": d["refundAmount"]}
                        for d in rule.get("ruleDetails", [])]
        refund_rules.append({"relations": relations, "ruleDetails": rule_details})
    sale_body = {
        "storeId": int(b.store_id),
        "activityId": pid,
        "refundSupport": src_sale["refundSupport"],
        "inventoryRecoverySupport": src_sale["inventoryRecoverySupport"],
        "refundTicketAllowTimes": src_sale["refundTicketAllowTimes"],
        "refundRuleConfigStatus": src_sale["refundRuleConfigStatus"],
        "purchaseStatus": src_sale["purchaseStatus"],
        "purchaseTimeStatus": src_sale["purchaseTimeStatus"],
        "tips": src_sale.get("tips", ""),
        "refundRuleList": refund_rules,
    }
    print(json.dumps(sale_body, ensure_ascii=False)[:500])
    r4 = b._request("POST", f"/v1/shop/product/{pid}/saleConfig",
                    params={"storeId": b.store_id}, json=sale_body)
    print("响应:", json.dumps(r4, ensure_ascii=False)[:300])

    print("=" * 70)
    print(f"★ 完成：888 店新项目 productId={pid}（复刻测试店 {SRC_PID}）")
    print("  请核对 B 端 UI 或等脚本回读 diff。")
    return pid


if __name__ == "__main__":
    sys.exit(main())
