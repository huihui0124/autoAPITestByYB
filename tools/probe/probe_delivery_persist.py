"""严验：deliveryType 各组合不只是"接口不报错"，还要看服务端真正落库的值。

上一步的 probe_delivery_matrix.py 只判断 PUT 是否抛异常 —— 服务端完全可能
接受请求但把非法/无意义的取值静默丢弃或归一化。这里对每个组合做：
    PUT -> 立刻 GET 回读 -> 对比落库值 -> 恢复原值
另外拉全量商品，统计真实存在的 deliveryType 组合分布（含 11 的实例有几个）。
"""
import json
import warnings
from collections import Counter

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


def get_detail(pid=PID):
    d = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity",
                   params={"storeId": c.store_id})
    return d.get("data") if isinstance(d, dict) and "data" in d else d


def read_dt(pid=PID):
    info = get_detail(pid).get("deliveryInfo") or {}
    return info.get("deliveryType"), info


def put(delivery_type):
    snap = get_detail()
    body = {k: v for k, v in snap.items() if k in SCHEMA_FIELDS}
    body.update(productId=PID, storeId=c.store_id)
    body["deliveryInfo"] = dict(body.get("deliveryInfo") or {})
    body["deliveryInfo"]["deliveryType"] = delivery_type
    try:
        c._request("PUT", api, params={"storeId": c.store_id, "productId": PID}, json=body)
        return True, ""
    except Exception as e:
        msg = str(e)
        if '"message"' in msg:
            try:
                msg = json.loads(msg[msg.index("{"):]).get("message", msg)
            except Exception:
                pass
        return False, msg[:60]


COMBOS = [
    ([4], "电子票"),
    ([11], "身份证件入场"),
    ([4, 11], "电子票 + 身份证件"),
    ([1, 11], "快递 + 身份证件"),
    ([3, 11], "自取 + 身份证件"),
    ([1, 3, 11], "快递+自取+身份证件"),
    ([4, 1, 11], "电子票+快递+身份证件"),
    ([4, 1], "电子票 + 快递（对照，已知拒绝）"),
]

print("=" * 100)
print(f"{'组合':38} {'接口':8} {'落库回读':22} 判定")
print("=" * 100)
rows = []
for combo, label in COMBOS:
    ok, msg = put(combo)
    stored, info = read_dt()
    if ok:
        same = sorted(stored or []) == sorted(combo)
        verdict = "✅ 真·接受（落库一致）" if same else f"⚠️ 静默改写 → {stored}"
    else:
        verdict = f"❌ 拒绝：{msg}"
        stored = None
    rows.append((label, combo, ok, stored, verdict))
    print(f"{label:38} {'200' if ok else '400':8} {str(stored):22} {verdict}")

print("\n恢复现场 ->", ORIG)
put(ORIG)
back, info = read_dt()
print("  复核 deliveryInfo:", json.dumps(info, ensure_ascii=False))

# ---- 全量真实数据：到底有没有 11 与其他值共存的实例 ----
print("\n" + "=" * 100)
print("全量商品 deliveryType 组合分布（测试环境）")
print("=" * 100)
all_rows = []
for pi in (1, 2, 3, 4):
    try:
        d = c._request("GET", "/v1/selfProduct/list",
                       params={"storeId": c.store_id, "pageIndex": pi, "pageSize": 100})
    except Exception:
        break
    r = d.get("data") if isinstance(d, dict) else d
    if isinstance(r, dict):
        r = r.get("data") or r.get("list") or []
    if not r:
        break
    all_rows += r
    if len(r) < 100:
        break

cnt = Counter(tuple(sorted(r.get("deliveryType") or [])) for r in all_rows)
print(f"共 {len(all_rows)} 个商品")
for combo, n in cnt.most_common():
    print(f"  {str(list(combo)):16} {n:4} 个")

with11 = [r for r in all_rows if 11 in (r.get("deliveryType") or [])]
print(f"\n含 11（身份证件入场）的商品：{len(with11)} 个")
for r in with11:
    print(f"  {r.get('productId'):>8} {str(r.get('productName'))[:36]:38} deliveryType={r.get('deliveryType')}")
