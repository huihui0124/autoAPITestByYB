"""重试上一步因网络超时未得出结论的组合，区分「超时」与「业务拒绝」。

上一步 probe_delivery_persist.py 里 [4] / [1,11] / [3,11] 都报
`HTTPConnectionPool(host='127.0.0.1', port=64746): Read timed out` —— 而 [4] 单值是
肯定合法的，说明这是本地代理超时，不是业务校验。这里对每个组合带重试地重跑，
并把「HTTP 状态码 / 业务 code / 落库回读」三者分开记录。
"""
import json
import time
import warnings

warnings.filterwarnings("ignore")
import os

# 本地代理（127.0.0.1:64746）对 B 端直连请求会偶发 read timeout，
# 而该域名本身可直连（curl --noproxy 200）。这里把它排除出代理，消除噪音。
os.environ["NO_PROXY"] = "ticket-test.miguvideo.com,127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

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
    for _ in range(3):
        try:
            d = c._request("GET", api, params={"storeId": c.store_id})
            return d.get("data") if isinstance(d, dict) and "data" in d else d
        except Exception:
            time.sleep(2)
    return {}


def read_dt():
    return (get_detail().get("deliveryInfo") or {}).get("deliveryType")


def put(delivery_type, tries=3):
    """返回 (状态, 说明)。状态: accepted / rejected / timeout"""
    snap = get_detail()
    if not snap:
        return "timeout", "GET 详情失败（拿不到快照，未写入）"
    body = {k: v for k, v in snap.items() if k in SCHEMA_FIELDS}
    body.update(productId=PID, storeId=c.store_id)
    body["deliveryInfo"] = dict(body.get("deliveryInfo") or {})
    body["deliveryInfo"]["deliveryType"] = delivery_type
    last = ""
    for i in range(tries):
        try:
            c._request("PUT", api, params={"storeId": c.store_id, "productId": PID},
                       json=body)
            return "accepted", ""
        except Exception as e:
            msg = str(e)
            if "message" in msg:
                try:
                    code = json.loads(msg[msg.index("{"):]).get("code")
                    m = json.loads(msg[msg.index("{"):]).get("message")
                    last = f"code={code} {m}"
                    return "rejected", last
                except Exception:
                    pass
            last = msg[:70]
            time.sleep(3)
    return "timeout", last


COMBOS = [
    ([4], "电子票（合法基线，用于判定超时与否）"),
    ([1, 11], "快递 + 身份证件入场"),
    ([3, 11], "自取 + 身份证件入场"),
    ([1, 3, 11], "快递 + 自取 + 身份证件入场"),
    ([4, 11], "电子票 + 身份证件入场（复测）"),
]

print("=" * 108)
print(f"{'组合':40} {'状态':10} {'落库回读':14} 说明")
print("=" * 108)
verdicts = []
for combo, label in COMBOS:
    st, note = put(combo)
    stored = read_dt() if st == "accepted" else None
    if st == "accepted":
        same = sorted(stored or []) == sorted(combo)
        note = "落库一致" if same else f"⚠️ 被静默改写为 {stored}"
    verdicts.append((label, combo, st, stored, note))
    print(f"{label:40} {st:10} {str(stored):14} {note}")

print("\n恢复现场 ->", ORIG)
st, note = put(ORIG)
print("  恢复:", st, note, "| 复核:", read_dt())

print("\n" + "=" * 108)
print("结论")
print("=" * 108)
for label, combo, st, stored, note in verdicts:
    icon = {"accepted": "✅ 接受", "rejected": "❌ 拒绝", "timeout": "⏱ 未知(超时)"}[st]
    print(f"  {icon:12} {label:38} {combo}")
