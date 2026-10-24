# -*- coding: utf-8 -*-
"""找到票档列表接口，看真实票档结构（salePrice / 库存 / 限购）。"""
import json, warnings
warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()


def get(api, **kw):
    p = {"storeId": c.store_id}
    p.update(kw)
    return c._request("GET", api, params=p)


CAND = [
    "/v2/shop/product/222915/event/ticket",
    "/v3/shop/product/222915/selfBuild/sku",
    "/v1/shopTrade/product/222915/eventTicketCategory",
    "/v2/shop/product/222915/onsale",
    "/v3/shop/product/222915/selfBuildActivity/events",
]
for api in CAND:
    try:
        d = get(api)
    except Exception as e:
        print(f"\n--- {api}\n    失败: {str(e)[:110]}")
        continue
    s = json.dumps(d, ensure_ascii=False)
    print(f"\n--- {api}   （{len(s)} 字符）")
    print(s[:1500])

# 找一个多票档商品
print("\n" + "=" * 90)
print("多票档商品排查")
print("=" * 90)
for pid, tag in [("222267", "多场次单票档限购测试yb"), ("222837", "电子票-条件退-非实名yb"),
                 ("222265", "售罄测试yb")]:
    for api in (f"/v2/shop/product/{pid}/event/ticket",):
        try:
            d = get(api)
            s = json.dumps(d, ensure_ascii=False)
            print(f"  {pid} {tag}: {s[:400]}")
        except Exception as e:
            print(f"  {pid} {tag}: {str(e)[:90]}")
