"""第十一轮：模型 → 接口 的映射（让层级归属可追溯）。"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()
doc = c.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
PATHS = doc["paths"]

TARGET_MODELS = [
    "SelfBuildActivityWebRequest", "AddEventWebReq", "SelfBuildEventsWebRequest",
    "AddTicketWebReq", "TicketEditWebReq", "SkuAgeLimitSaveRequest",
    "SkuSelfAdditionDto", "ShopSelfBuildGoodsWebRequest",
    "ShopSelfBuildGoodsSkuUpdateWebRequest", "DeliveryInfoRequest",
    "EventEditWebReq", "TicketQueryWebReq", "EventQueryReq",
    "ActivityRefundRuleDetailFullDTO",
]

use = {}
for api, ops in PATHS.items():
    for m, op in ops.items():
        rb = op.get("requestBody")
        if not rb:
            continue
        for _ct, v in rb.get("content", {}).items():
            raw = json.dumps(v.get("schema", {}))
            for name in TARGET_MODELS:
                if name in raw:
                    use.setdefault(name, []).append(f"{m.upper()} {api}")

print("=" * 84)
print("请求模型 → 接口")
print("=" * 84)
for name in TARGET_MODELS:
    print(f"\n<{name}>")
    for x in use.get(name, ["  (未被任何接口的请求体直接引用)"]):
        print("   ", x)

print("\n" + "=" * 84)
print("退款规则接口签名")
print("=" * 84)
for api in ["/v1/shop/product/{activityId}/saleConfig"]:
    for m, op in (PATHS.get(api) or {}).items():
        print(f"\n{m.upper()} {api}  tags={op.get('tags')}")
        for p in op.get("parameters", []):
            print(f"   · {p.get('name')} in={p.get('in')} required={p.get('required')} "
                  f"{json.dumps(p.get('schema', {}), ensure_ascii=False)[:70]}")
        rb = op.get("requestBody")
        if rb:
            for ct, v in rb.get("content", {}).items():
                print(f"   body = {json.dumps(v.get('schema'), ensure_ascii=False)[:200]}")

print("\n" + "=" * 84)
print("ActivityRefundRuleDetailFullDTO 字段")
print("=" * 84)
for f, v in (doc["components"]["schemas"].get("ActivityRefundRuleDetailFullDTO", {}).get("properties") or {}).items():
    print(f"   {f}: {v.get('type','?')} 枚举={v.get('enum')} {(v.get('description') or '')[:60]}")
