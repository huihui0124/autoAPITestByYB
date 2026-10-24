"""第十轮：校验链 + 枚举，正确解析服务端 message。"""
import json
import re
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()
print("环境:", b_env.name)


def call(payload):
    """返回 (ok, message_or_data, raw)"""
    try:
        d = c._request("POST", "/v1/selfProduct/add", json=payload)
        return True, d, json.dumps(d, ensure_ascii=False)
    except Exception as e:
        raw = str(e)
        m = re.search(r"message\\?\"?\s*:\s*\"([^\"]+)\"", raw)
        if not m:
            m = re.search(r"message:\s*([^,}]+)", raw)
        return False, (m.group(1) if m else raw[-160:]), raw


VENUE_POI = 115671
base = {
    "storeId": c.store_id,
    "activityName": "__enum_probe__",
    "categoryId": 1,
    "superCategoryId": 1,
    "displayType": 1,
    "city": {"poiValueId": VENUE_POI},
    "venue": {"poiValueId": VENUE_POI},
    "poster": "https://img.test/p.png",
    "activityDesc": "probe",
    "visible": True,
    "deliveryInfo": {"deliveryType": [99999]},
    "needRealNameInfo": 99999,
}

STEPS = [
    ("supportSelectSeat", False),
    ("thirdVerificationType", "Paid_After"),
    ("canConsumeAfterEndTime", 0),
    ("ticketTips", "t"),
    ("purchaseTips", "t"),
    ("recommendWords", "probe"),
    ("activityDuration", "60"),
    ("popTipsSwitch", False),
    ("whitelistSwitch", False),
    ("goodsTagInfo", {"tagIds": []}),
    ("goodsGroupInfo", {"groupIds": []}),
    ("customerServiceIdList", []),
    ("activityVideo", {"url": "", "addition": ""}),
]

print("\n[A] 校验链：逐个补字段，直到不再报「不可为空」")
payload = dict(base)
for tag, val in STEPS:
    payload[tag] = val
    ok, msg, raw = call(payload)
    print(f"  + {tag:24} -> {'OK' if ok else msg}")
    if ok:
        break

print("\n[B] deliveryType 候选")
for dt in ([4], [1], [3], [11], [1, 3], [99999]):
    p = dict(payload)
    p["deliveryInfo"] = {"deliveryType": dt}
    ok, msg, raw = call(p)
    print(f"  deliveryType={str(dt):10} -> {'建成功' if ok else msg}")
    if ok:
        print("     raw:", raw[:200])

print("\n[C] needRealNameInfo 候选")
for rn in (0, 1, 2, 3, 99):
    p = dict(payload)
    p["deliveryInfo"] = {"deliveryType": [4]}
    p["needRealNameInfo"] = rn
    ok, msg, raw = call(p)
    print(f"  needRealNameInfo={rn:3} -> {'建成功' if ok else msg}")
    if ok:
        print("     raw:", raw[:200])
        break
