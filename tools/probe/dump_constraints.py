"""第三轮：确认各层必填约束、枚举取值、以及中文描述里的口径。"""
import json
import re
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

client = BAdminClient()
doc = client.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
SCHEMAS = doc["components"]["schemas"]
PATHS = doc["paths"]

print("=" * 84)
print("[A] 各层请求模型的 required 约束")
print("=" * 84)
for name in ["SelfBuildActivityWebRequest", "AddEventWebReq", "Event", "EventEditWebReq",
             "AddTicketWebReq", "TicketEditWebReq", "SkuAgeLimitSaveRequest",
             "SkuAgeLimitConfigDTO", "SkuAgeLimitRuleDTO", "SkuSelfAdditionDto",
             "ShopSelfBuildGoodsWebRequest", "ShopSelfBuildGoodsSkuUpdateWebRequest",
             "DeliveryInfoRequest"]:
    sch = SCHEMAS.get(name)
    if not sch:
        print(f"  <{name}> 不存在")
        continue
    print(f"  <{name}>  required = {sch.get('required')}")

print("\n" + "=" * 84)
print("[B] 相关模型全字段")
print("=" * 84)
for name in ["Event", "EventEditWebReq", "ProductEventDTO", "SelfBuildActivityResponse",
             "ShopSelfBuildGoodsWebRequest", "ShopSelfBuildGoodsSkuUpdateWebRequest",
             "ShopSelfBuildProductVO", "DeliveryInfoRequest"]:
    sch = SCHEMAS.get(name)
    if not sch:
        print(f"\n<{name}> 不存在")
        continue
    print(f"\n<{name}>")
    for fname, val in (sch.get("properties") or {}).items():
        enum = val.get("enum")
        en = f" 枚举={enum}" if enum else ""
        desc = (val.get("description") or "").strip().replace("\n", " ")[:66]
        print(f"    {fname}: {val.get('type','?')}{en}  {desc}")

print("\n" + "=" * 84)
print("[C] 中文描述里含口径关键词的字段（学枚举含义）")
print("=" * 84)
KW = ["实名", "限购", "证件", "售卖", "选座", "核销", "邮寄", "快递", "自取", "身份证", "纸质"]
seen = set()
for sname, sch in SCHEMAS.items():
    for fname, val in (sch.get("properties") or {}).items():
        desc = (val.get("description") or "")
        if any(k in desc for k in KW) and (sname, fname) not in seen:
            seen.add((sname, fname))
            print(f"    {sname}.{fname}: {val.get('type','?')}  {desc.strip().replace(chr(10),' ')[:90]}")

print("\n" + "=" * 84)
print("[D] ageLimit / tickets/list / events/list 的入参")
print("=" * 84)
for api in ["/v1/selfProduct/tickets/ageLimit", "/v1/selfProduct/tickets/list",
            "/v1/selfProduct/events/list", "/v1/selfProduct/list",
            "/v2/shop/product/{productId}/event/ticket"]:
    for method, op in (PATHS.get(api) or {}).items():
        print(f"\n  {method.upper()} {api}")
        ps = op.get("parameters", [])
        if not ps:
            print("      (无显式参数)")
        for p in ps:
            print(f"      · {p.get('name')} in={p.get('in')} required={p.get('required')} "
                  f"{json.dumps(p.get('schema', {}), ensure_ascii=False)[:70]}")
        rb = op.get("requestBody")
        if rb:
            for ct, v in rb.get("content", {}).items():
                print(f"      body[{ct}] = {json.dumps(v.get('schema'), ensure_ascii=False)[:130]}")
