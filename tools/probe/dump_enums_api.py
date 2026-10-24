"""第五轮：直接调用 B 端字典接口，拿枚举官方口径。"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()

print("=" * 84)
print("[A] GET /v1/config/enums  —— 字典清单")
print("=" * 84)
try:
    data = c._request("GET", "/v1/config/enums")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
except Exception as e:
    print("  失败:", str(e)[:300])

print("\n" + "=" * 84)
print("[B] 逐个候选字典")
print("=" * 84)
CAND = ["deliveryType", "needRealNameInfo", "realNameType", "ticketIssueWay",
        "thirdVerificationType", "displayType", "saleStatus", "maxVerifyCount",
        "refundSupport", "delivery_type", "need_real_name_info"]
for name in CAND:
    try:
        d = c._request("GET", f"/v1/config/enums/{name}")
        print(f"\n  [{name}] -> {json.dumps(d, ensure_ascii=False)[:400]}")
    except Exception as e:
        print(f"\n  [{name}] 失败: {str(e)[:120]}")
