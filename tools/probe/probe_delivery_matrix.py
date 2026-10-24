"""穷举 deliveryType 的合法组合，得出「交付方式」维度的真实拆分依据。

方法与 probe_delivery_combo.py 相同：PUT 到测试商品 222841，逐个组合试，最后恢复原值。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

PID = 222841
ORIG = [1, 3]
SCHEMA_FIELDS = {
    "productId", "storeId", "activityName", "categoryId", "superCategoryId", "city",
    "venue", "displayType", "activityVideo", "poster", "activityDesc", "deliveryInfo",
    "goodsTagInfo", "goodsGroupInfo", "recommendWords", "activityDuration", "ticketTips",
    "purchaseTips", "purchaseNoticeTemplateId", "visible", "supportSelectSeat",
    "popTipsSwitch", "popTipsTitle", "popTipsContent", "needRealNameInfo",
    "thirdVerificationType", "canConsumeAfterEndTime", "customerServiceIdList",
    "whitelistSwitch", "channelId", "channelType", "whitelistUsers",
}

c = BAdminClient()
api = f"/v3/shop/product/{PID}/selfBuildActivity"


def get_detail():
    d = c._request("GET", api, params={"storeId": c.store_id})
    return d.get("data") if isinstance(d, dict) and "data" in d else d


def put(delivery_type):
    snap = get_detail()
    body = {k: v for k, v in snap.items() if k in SCHEMA_FIELDS}
    body.update(productId=PID, storeId=c.store_id)
    body["deliveryInfo"] = dict(body.get("deliveryInfo") or {})
    body["deliveryInfo"]["deliveryType"] = delivery_type
    try:
        c._request("PUT", api, params={"storeId": c.store_id, "productId": PID}, json=body)
        return True, "接受"
    except Exception as e:
        msg = str(e)
        if '"message"' in msg:
            try:
                msg = json.loads(msg[msg.index("{"):]).get("message", msg)
            except Exception:
                pass
        return False, msg[:70]


COMBOS = [
    ([4], "电子票"),
    ([1], "快递"),
    ([3], "自取"),
    ([11], "身份证件入场"),
    ([1, 3], "快递 + 自取"),
    ([4, 1], "电子票 + 快递"),
    ([4, 3], "电子票 + 自取"),
    ([4, 11], "电子票 + 身份证件入场"),
    ([1, 11], "快递 + 身份证件入场"),
    ([3, 11], "自取 + 身份证件入场"),
    ([1, 3, 11], "快递 + 自取 + 身份证件入场"),
]

print("=" * 88)
print(f"{'组合':34} {'结果':6} 服务端返回")
print("=" * 88)
results = []
for combo, label in COMBOS:
    ok, msg = put(combo)
    results.append((label, combo, ok, msg))
    print(f"{label:34} {'✅ 接受' if ok else '❌ 拒绝':6} {'' if ok else msg}")

print("\n恢复现场 ->", ORIG)
print("  恢复结果:", put(ORIG))
print("  复核:", json.dumps(get_detail().get("deliveryInfo"), ensure_ascii=False))

print("\n" + "=" * 88)
print("合法组合汇总")
print("=" * 88)
for label, combo, ok, msg in results:
    print(f"  {'✅' if ok else '❌'} {label:32} {combo}")
