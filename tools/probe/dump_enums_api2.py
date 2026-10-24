"""第六轮：探测 /v1/config/enums 的调用形式与字典名。"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()

# 1) 看接口签名
from migu_api.config import b_env
doc = c.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
for api in ["/v1/config/enums", "/v1/config/enums/{name}"]:
    for m, op in (doc["paths"].get(api) or {}).items():
        print(f"\n{m.upper()} {api}")
        print("  summary:", op.get("summary"))
        for p in op.get("parameters", []):
            print(f"  · {p.get('name')} in={p.get('in')} required={p.get('required')} "
                  f"desc={p.get('description')} schema={json.dumps(p.get('schema', {}), ensure_ascii=False)[:80]}")

print("\n" + "=" * 84)
print("尝试多种字典名 / 调用形式")
print("=" * 84)
NAMES = ["DELIVERY_TYPE", "DeliveryType", "deliveryTypeEnum", "DeliverType",
         "NEED_REAL_NAME_INFO", "NeedRealNameInfo", "realNameType", "RealNameType",
         "TICKET_ISSUE_WAY", "TicketIssueWay", "displayType", "DISPLAY_TYPE",
         "thirdVerificationType", "THIRD_VERIFICATION_TYPE", "refundSupport"]
for n in NAMES:
    for method, kw in [("GET", {"params": {}}), ("GET", {"params": {"storeId": c.store_id}})]:
        try:
            d = c._request(method, f"/v1/config/enums/{n}", **kw)
            if d:
                print(f"  [{n}] {json.dumps(d, ensure_ascii=False)[:300]}")
                break
        except Exception as e:
            msg = str(e)[:80]
            if "404" not in msg and "not found" not in msg.lower():
                pass
print("  （以上只打印有返回值的）")

print("\n尝试 /v1/config/enums?names=...")
for names in ["deliveryType,needRealNameInfo", "DELIVERY_TYPE"]:
    try:
        d = c._request("GET", "/v1/config/enums", params={"names": names})
        print(f"  [{names}] -> {json.dumps(d, ensure_ascii=False)[:300]}")
    except Exception as e:
        print(f"  [{names}] 失败 {str(e)[:80]}")

print("\n尝试 /v1/config（含 storeId）")
for params in [{}, {"storeId": c.store_id}]:
    try:
        d = c._request("GET", "/v1/config", params=params)
        print(f"  params={params} -> {json.dumps(d, ensure_ascii=False)[:600]}")
    except Exception as e:
        print(f"  params={params} 失败 {str(e)[:80]}")
