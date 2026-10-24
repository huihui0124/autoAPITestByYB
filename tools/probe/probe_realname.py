# -*- coding: utf-8 -*-
"""确认项目级互斥单值字段的取值域：needRealNameInfo / deliveryType / supportSelectSeat。"""
import json, warnings
warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()
doc = c.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()

rows = c._request("GET", "/v1/selfProduct/list", params={"storeId": c.store_id})
if isinstance(rows, dict):
    rows = rows.get("data") or rows.get("list") or []
print(f"商品数 {len(rows)}\n")

print("=" * 78)
print("① /v1/selfProduct/list 单条商品的全部字段")
print("=" * 78)
first = rows[0]
for k, v in first.items():
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    print(f"  {k:36} = {s[:70]}")

print("\n" + "=" * 78)
print("② 找 needRealNameInfo 类字段并对比不同实名商品")
print("=" * 78)
cand = [k for k in first if any(w in k.lower() for w in ("real", "name", "cert", "credential"))]
print("候选字段:", cand)

want = {"222915": "一单一证", "222617": "实名制", "222261": "非实名",
        "222305": "非实名", "222337": "实名制", "222339": "实名测试",
        "222619": "身份证入场", "2222415": "?"}
by_id = {str(r.get("productId")): r for r in rows}
print(f"\n{'productId':>9} {'商品名':30} {'用户标注':10} " + " ".join(f"{k[:22]:24}" for k in cand))
for pid, tag in want.items():
    r = by_id.get(pid)
    if not r:
        continue
    vals = " ".join(f"{str(r.get(k))[:22]:24}" for k in cand)
    print(f"{pid:>9} {str(r.get('productName'))[:28]:30} {tag:12} {vals}")

print("\n" + "=" * 78)
print("③ 找商品「详情」类 GET 接口（可能返回 needRealNameInfo）")
print("=" * 78)
for p in sorted(doc["paths"]):
    for m, op in doc["paths"][p].items():
        if m != "get":
            continue
        lp = p.lower()
        if "product" in lp and ("{" in p or "detail" in lp or "info" in lp):
            print(f"  GET {p:66} {op.get('summary','')[:36]}")
