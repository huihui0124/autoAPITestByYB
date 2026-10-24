"""第九轮：继续走项目级校验链，观察 deliveryType / needRealNameInfo 的枚举校验。
若意外建成功，立即删除该探测商品（只在测试环境）。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()
print("环境:", b_env.name)

VENUE_POI = 115671
payload = {
    "storeId": c.store_id,
    "activityName": "__enum_probe__",
    "categoryId": 1,
    "superCategoryId": 1,
    "displayType": 1,
    "city": {"poiValueId": VENUE_POI},
    "venue": {"poiValueId": VENUE_POI},
    "poster": "https://img.test/p.png",
    "visible": True,
    "deliveryInfo": {"deliveryType": [99999]},
    "needRealNameInfo": 99999,
}

STEPS = [
    ("supportSelectSeat", False),
    ("thirdVerificationType", "Paid_After"),
    ("canConsumeAfterEndTime", 0),
    ("ticketTips", "tips"),
    ("purchaseTips", "tips"),
    ("recommendWords", "probe"),
    ("activityDuration", "60"),
    ("popTipsSwitch", False),
    ("whitelistSwitch", False),
    ("goodsTagInfo", {"tagIds": []}),
    ("goodsGroupInfo", {"groupIds": []}),
    ("customerServiceIdList", []),
]

print("\n[A] 逐项补齐，观察报错变化")
last = None
for tag, val in STEPS:
    payload[tag] = val
    try:
        d = c._request("POST", "/v1/selfProduct/add", json=payload)
        print(f"  + {tag:24} -> 成功: {json.dumps(d, ensure_ascii=False)[:220]}")
        last = d
        break
    except Exception as e:
        msg = str(e).split(":")
        short = msg[-1].strip()[:160]
        print(f"  + {tag:24} -> {short}")

print("\n[B] 单独把 deliveryType 改成合法候选，看是否还有别的要求")
for dt in ([4], [1], [3], [11], [1, 3]):
    p = dict(payload)
    p["deliveryInfo"] = {"deliveryType": dt}
    try:
        d = c._request("POST", "/v1/selfProduct/add", json=p)
        print(f"  deliveryType={dt} -> 成功: {json.dumps(d, ensure_ascii=False)[:220]}")
        last = d
    except Exception as e:
        print(f"  deliveryType={dt} -> {str(e).split(':')[-1].strip()[:150]}")

print("\n[C] needRealNameInfo 候选值")
for rn in (0, 1, 2, 3):
    p = dict(payload)
    p["deliveryInfo"] = {"deliveryType": [4]}
    p["needRealNameInfo"] = rn
    try:
        d = c._request("POST", "/v1/selfProduct/add", json=p)
        print(f"  needRealNameInfo={rn} -> 成功: {json.dumps(d, ensure_ascii=False)[:220]}")
        last = d
    except Exception as e:
        print(f"  needRealNameInfo={rn} -> {str(e).split(':')[-1].strip()[:150]}")

print("\n[D] 收尾：若建出了探测商品，删掉")
if isinstance(last, dict) and last.get("data"):
    pid = last["data"] if isinstance(last["data"], int) else (last["data"] or {}).get("productId")
    print("  建出的 productId =", pid)
    if pid:
        try:
            c._request("DELETE", f"/v1/shopTrade/product/{pid}", params={"storeId": c.store_id})
            print("  已删除")
        except Exception as e:
            print("  删除失败:", str(e)[:200])
