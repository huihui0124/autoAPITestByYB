# -*- coding: utf-8 -*-
"""读真实商品的完整三层配置：项目 / 场次 / 票档。用于确认层级归属与字段取值。"""
import json, warnings
warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()

TARGETS = {
    "222915": "电子票-一单一证yb",
    "222617": "电子票-实名制yb",
    "222261": "电子票非实名yb",
    "222305": "选座-电子票-非实名yb",
    "222339": "选座-快递-实名测试yb",
    "222619": "身份证入场测试yb",
    "222249": "仅自取项目yb",
    "222841": "纸质票验证售后审批yb",
}
DIM = ["activityName", "productName", "needRealNameInfo", "supportSelectSeat", "deliveryType",
       "thirdVerificationType", "canConsumeAfterEndTime", "whitelistSwitch", "visible",
       "channelId", "channelType", "refundSupport", "spuLevelLimitBuyNum", "displayType",
       "maxVerifyCount", "salePrice", "totalStockNum", "limitBuyNum"]

print("=" * 110)
print("① 项目级配置横向对比（GET /v3/shop/product/{id}/selfBuildActivity）")
print("=" * 110)
proj = {}
for pid, tag in TARGETS.items():
    try:
        d = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity", params={"storeId": c.store_id})
        proj[pid] = (d if isinstance(d, dict) else {}, tag)
    except Exception as e:
        proj[pid] = ({"_err": str(e)[:80]}, tag)

cols = list(proj)
print(f"{'字段':26} | " + " | ".join(f"{p[-6:]:>18}" for p in cols))
for k in DIM:
    vals = []
    for p in cols:
        d = proj[p][0]
        v = d.get(k, "-")
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        vals.append(str(v)[:18])
    if len(set(vals)) > 1 or vals[0] != "-":
        print(f"{k:26} | " + " | ".join(f"{v:>18}" for v in vals))
print("\n列对应: " + ", ".join(f"{p}={proj[p][1]}" for p in cols))

pid = "222915"
print("\n" + "=" * 110)
print(f"② 完整三层结构样例（商品 {pid} {TARGETS[pid]}）")
print("=" * 110)
print("\n--- 项目级 selfBuildActivity 全部字段 ---")
for k, v in proj[pid][0].items():
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    print(f"  {k:30} = {s[:88]}")

for label, api in [("场次级", f"/v3/shop/product/{pid}/selfBuildActivity/events"),
                   ("票档级", f"/v3/shop/product/{pid}/selfBuild")]:
    print(f"\n--- {label} {api} ---")
    try:
        d = c._request("GET", api, params={"storeId": c.store_id})
    except Exception as e:
        print("  失败:", str(e)[:100]); continue
    rows = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else d)
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("records") or [rows]
    print(f"  共 {len(rows) if isinstance(rows, list) else 1} 条")
    for r in (rows or [])[:6]:
        if isinstance(r, dict):
            print("   ", json.dumps({k: v for k, v in r.items()
                                     if not isinstance(v, (dict, list))}, ensure_ascii=False)[:240])
