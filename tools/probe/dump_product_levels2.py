# -*- coding: utf-8 -*-
"""① 读真实商品的场次/票档原始结构  ② 统计 needRealNameInfo 取值分布（确认实名档位数）"""
import json, warnings
from collections import Counter
warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()


def get(api):
    return c._request("GET", api, params={"storeId": c.store_id})


print("=" * 100)
print("① 真实商品的场次 / 票档原始返回（商品 222915 电子票-一单一证yb）")
print("=" * 100)
for label, api in [("场次", "/v3/shop/product/222915/selfBuildActivity/events"),
                   ("票档", "/v3/shop/product/222915/selfBuild")]:
    try:
        d = get(api)
    except Exception as e:
        print(f"\n--- {label} 失败: {str(e)[:110]}")
        continue
    print(f"\n--- {label} {api} ---")
    print(json.dumps(d, ensure_ascii=False)[:1600])

print("\n" + "=" * 100)
print("② 「多场次 / 多票档」实证")
print("=" * 100)
for pid, tag in [("222267", "多场次单票档限购测试yb"), ("222915", "电子票-一单一证yb"),
                 ("222305", "选座-电子票-非实名yb"), ("222337", "非选座-快递-实名制yb")]:
    try:
        ev = get(f"/v3/shop/product/{pid}/selfBuildActivity/events")
        tk = get(f"/v3/shop/product/{pid}/selfBuild")
    except Exception as e:
        print(f"  {pid} {tag}: {str(e)[:80]}"); continue

    def cnt(x):
        if isinstance(x, list):
            return len(x)
        if isinstance(x, dict):
            for k in ("data", "list", "records"):
                if isinstance(x.get(k), list):
                    return len(x[k])
        return "?"

    print(f"  {pid} {tag:26} 场次数={cnt(ev)}  票档数={cnt(tk)}")

print("\n" + "=" * 100)
print("③ needRealNameInfo 取值分布（前 100 个商品）")
print("=" * 100)
rows = []
d = c._request("GET", "/v1/selfProduct/list",
               params={"storeId": c.store_id, "pageIndex": 1, "pageSize": 100})
r = d.get("data") if isinstance(d, dict) else d
if isinstance(r, dict):
    r = r.get("data") or r.get("list") or []
rows = r or []
print(f"取到 {len(rows)} 个商品，逐个读项目级配置…")
cnt, samples = Counter(), {}
for r in rows:
    pid = r.get("productId")
    try:
        d = get(f"/v3/shop/product/{pid}/selfBuildActivity")
    except Exception:
        cnt["读取失败"] += 1
        continue
    v = (d or {}).get("needRealNameInfo")
    cnt[v] += 1
    samples.setdefault(v, []).append((pid, (r.get("productName") or "")[:22]))
print("\nneedRealNameInfo 分布:", dict(cnt))
for v, lst in sorted(samples.items(), key=lambda x: str(x[0])):
    print(f"  值={v!r:6} 共 {len(lst):3} 个   样例: " +
          ", ".join(f"{p}:{n}" for p, n in lst[:6]))
