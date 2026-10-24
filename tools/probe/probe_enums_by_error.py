"""第七轮：用非法值触发服务端校验，反查 deliveryType / needRealNameInfo 的合法取值域。
只在测试环境执行；请求故意缺字段，预期被校验拒绝，不会产生商品。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

print("当前环境:", b_env.name, b_env.base_url if hasattr(b_env, "base_url") else "")
c = BAdminClient()

BASE = {
    "storeId": c.store_id,
    "activityName": "__enum_probe_do_not_keep__",
    "categoryId": 1,
    "superCategoryId": 1,
    "displayType": 1,
    "deliveryInfo": {"deliveryType": [99999]},
    "needRealNameInfo": 99999,
}


def probe(payload, tag):
    try:
        d = c._request("POST", "/v1/selfProduct/add", json=payload)
        print(f"  [{tag}] 竟然成功 -> {json.dumps(d, ensure_ascii=False)[:260]}")
    except Exception as e:
        print(f"  [{tag}] 被拒 -> {str(e)[:400]}")


print("\n[1] deliveryType / needRealNameInfo 同时给非法值")
probe(BASE, "both-invalid")

print("\n[2] 只让 deliveryType 非法")
p = dict(BASE)
p["needRealNameInfo"] = 0
probe(p, "deliveryType=99999")

print("\n[3] 只让 needRealNameInfo 非法")
p = dict(BASE)
p["deliveryInfo"] = {"deliveryType": [4]}
probe(p, "needRealNameInfo=99999")

print("\n[4] 完全空 body（看是否回显字段要求）")
probe({}, "empty")
