"""核实 deliveryType 的真实取值域：能否一个项目同时挂 电子票/快递/自取。

结论用途：决定「交付方式」是否必须拆项目。
"""
import json
import warnings
from collections import Counter

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()

# ---------- 1. 全量商品的 deliveryType 分布 ----------
rows = []
for pi in (1, 2, 3, 4):
    d = c._request("GET", "/v1/selfProduct/list",
                   params={"storeId": c.store_id, "pageIndex": pi, "pageSize": 100})
    r = d.get("data") if isinstance(d, dict) else d
    if isinstance(r, dict):
        r = r.get("data") or r.get("list") or []
    if not r:
        break
    rows += r
    if len(r) < 100:
        break

print(f"=== 商品总数 {len(rows)} ===")
print("\n=== deliveryType 取值分布（列表接口）===")
cnt = Counter()
sample = {}
for r in rows:
    dt = r.get("deliveryType")
    k = json.dumps(dt, ensure_ascii=False)
    cnt[k] += 1
    sample.setdefault(k, []).append(r.get("productName"))
for k, v in cnt.most_common():
    print(f"  {k:14} {v:4} 个   例: {', '.join(str(x)[:22] for x in sample[k][:3])}")

# ---------- 2. 是否存在 电子票(4) 与其他方式共存的商品 ----------
print("\n=== 含 4（电子票）的商品里，是否有同时含 1/3/11 的 ===")
hit = []
for r in rows:
    dt = r.get("deliveryType")
    if not isinstance(dt, list):
        continue
    norm = set()
    for x in dt:
        if isinstance(x, dict):
            norm.add(x.get("deliveryType") or x.get("type") or x.get("value"))
        else:
            norm.add(x)
    if 4 in norm and (norm & {1, 3, 11}):
        hit.append((r.get("productId"), r.get("productName"), dt))
print(f"  命中 {len(hit)} 个")
for h in hit[:10]:
    print("   ", h)

# ---------- 3. 详情接口看单个商品的 deliveryInfo 原文 ----------
print("\n=== 详情接口 deliveryInfo 原文（挑多值商品）===")
seen = set()
for r in rows:
    dt = r.get("deliveryType")
    if not isinstance(dt, list) or len(dt) < 2:
        continue
    pid = r.get("productId")
    if not pid or pid in seen:
        continue
    seen.add(pid)
    try:
        d = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity",
                       params={"storeId": c.store_id})
    except Exception as e:
        print(f"  {pid} 详情失败: {str(e)[:60]}")
        continue
    d = (d.get("data") if isinstance(d, dict) and "data" in d else d) or {}
    print(f"\n  --- {pid} {r.get('productName')} ---")
    print("   deliveryInfo:", json.dumps(d.get("deliveryInfo"), ensure_ascii=False)[:600])
    print("   deliveryType(顶层):", json.dumps(d.get("deliveryType"), ensure_ascii=False)[:200])
    print("   needRealNameInfo:", d.get("needRealNameInfo"),
          "| supportSelectSeat:", d.get("supportSelectSeat"))
    if len(seen) >= 5:
        break

# ---------- 4. 请求模型：deliveryInfo 的完整定义 ----------
print("\n=== OpenAPI 里 deliveryInfo 相关 schema ===")
doc = c.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
S = doc["components"]["schemas"]
for n, s in S.items():
    txt = json.dumps(s, ensure_ascii=False)
    if "deliveryType" in txt and "properties" in s:
        props = s["properties"].get("deliveryType")
        if props:
            print(f"\n  <{n}>.deliveryType = {json.dumps(props, ensure_ascii=False)}")
print("\n=== 名称含 Delivery 的 model ===")
for n in sorted(S):
    if "deliver" in n.lower():
        p = S[n].get("properties") or {}
        print(f"  {n}: {', '.join(p.keys())[:220]}")
