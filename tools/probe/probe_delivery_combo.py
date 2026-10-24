"""决定性验证：一个项目能否同时挂 电子票(4) + 快递(1) + 自取(3)？

做法：拿测试环境自建商品 222841（纸质票验证售后审批yb，当前 deliveryType=[1,3]）
      走 PUT /v3/shop/product/{productId}/selfBuildActivity，
      把 deliveryType 改成 [1,3,4]，看服务端是接受还是拒绝。
      无论结果如何，脚本末尾都会把配置恢复成 [1,3] 并复核。

安全措施：先 GET 快照 → 只回写 schema 里存在的字段 → 改完立即恢复 → 复核。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

PID = 222841
BACKUP = "/tmp/p222841_before.json"
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


def put(delivery_type, tag):
    snap = get_detail()
    json.dump(snap, open(BACKUP, "w"), ensure_ascii=False, indent=1)
    body = {k: v for k, v in snap.items() if k in SCHEMA_FIELDS}
    body["productId"] = PID
    body["storeId"] = c.store_id
    body["deliveryInfo"] = dict(body.get("deliveryInfo") or {})
    body["deliveryInfo"]["deliveryType"] = delivery_type
    print(f"\n--- {tag}：PUT deliveryType={delivery_type} ---")
    print("  body 字段数:", len(body), "| deliveryInfo:",
          json.dumps(body["deliveryInfo"], ensure_ascii=False))
    try:
        r = c._request("PUT", api, params={"storeId": c.store_id, "productId": PID}, json=body)
        print("  ✅ 服务端接受，返回:", json.dumps(r, ensure_ascii=False)[:300])
        return True
    except Exception as e:
        print("  ❌ 服务端拒绝:", str(e)[:400])
        return False


def verify():
    d = get_detail()
    print("  复核 deliveryInfo:", json.dumps(d.get("deliveryInfo"), ensure_ascii=False))
    print("  复核 activityName:", d.get("activityName"),
          "| needRealNameInfo:", d.get("needRealNameInfo"),
          "| supportSelectSeat:", d.get("supportSelectSeat"))
    return d.get("deliveryInfo", {}).get("deliveryType")


print("=" * 70)
print("起点状态")
print("=" * 70)
before = verify()

print("\n" + "=" * 70)
print("实验：电子票(4) 能否与 快递(1)/自取(3) 共存于同一项目")
print("=" * 70)
ok = put([1, 3, 4], "实验组")
accept = verify() if ok else None

print("\n" + "=" * 70)
print("恢复现场")
print("=" * 70)
put(before if isinstance(before, list) else [1, 3], "恢复")
after = verify()

print("\n" + "=" * 70)
print("结论")
print("=" * 70)
print(f"  原值 {before} → 实验 [1,3,4] {'被接受' if ok else '被拒绝'} → 恢复后 {after}")
print("  恢复成功" if after == before else "  ⚠️ 恢复失败，请手工检查 /tmp/p222841_before.json")
