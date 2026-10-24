"""第八轮：拿到合法场馆 poiValueId，然后顺着校验链把「项目级必填项」走完。
用非法 deliveryType/needRealNameInfo 触发枚举校验，看服务端是否回显允许值。
只在测试环境，且所有请求都故意不合法（预期 400），不会真正建出商品。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()
print("环境:", b_env.name)

print("\n[A] 取场馆")
venue_poi = None
for api, kw in [("/v1/venue", {"params": {"storeId": c.store_id}}),
                ("/v2/venue", {"params": {"storeId": c.store_id}})]:
    try:
        d = c._request("GET", api, **kw)
        rows = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else None)
        print(f"  {api} -> {json.dumps(d, ensure_ascii=False)[:400]}")
        if isinstance(rows, list) and rows:
            print("  第一条键:", sorted(rows[0].keys())[:20])
            for k in ("poiValueId", "id", "venueId", "poiId"):
                if k in rows[0]:
                    venue_poi = rows[0][k]
                    break
            print("  推测场地 poiValueId =", venue_poi)
            break
    except Exception as e:
        print(f"  {api} 失败: {str(e)[:150]}")

print("\n[B] 顺校验链走（每轮补一个字段）")
payload = {
    "storeId": c.store_id,
    "activityName": "__enum_probe__",
    "categoryId": 1,
    "superCategoryId": 1,
    "displayType": 1,
    "deliveryInfo": {"deliveryType": [99999]},
    "needRealNameInfo": 99999,
}
# 逐步注入，观察错误如何变化
INJECT = [
    ("city", {"poiValueId": venue_poi or 1}),
    ("venue", {"poiValueId": venue_poi or 1}),
    ("poster", "https://img.test/p.png"),
    ("activityDesc", "probe"),
    ("visible", True),
]
seen_msgs = []
for tag, val in INJECT:
    payload[tag] = val
    try:
        d = c._request("POST", "/v1/selfProduct/add", json=payload)
        print(f"  加了 {tag:14} -> 成功？{json.dumps(d, ensure_ascii=False)[:200]}")
    except Exception as e:
        msg = str(e)
        print(f"  加了 {tag:14} -> {msg[:200]}")
        if msg not in seen_msgs:
            seen_msgs.append(msg)
print("\n[C] 错误信息演进（去重）")
for m in seen_msgs:
    print("  -", m[:200])
