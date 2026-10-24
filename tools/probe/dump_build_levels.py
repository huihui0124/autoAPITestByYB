"""按「项目 / 场次 / 票档」三层，展开 B 端建站接口的请求字段归属。

结论来源：B 端 /v3/api-docs（接口签名 + 请求模型），不是从现有商品反推。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

client = BAdminClient()
doc = client.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
SCHEMAS = doc["components"]["schemas"]
PATHS = doc["paths"]


def ref_name(node):
    return (node or {}).get("$ref", "").split("/")[-1]


def get_body(api, method="post"):
    op = PATHS.get(api, {}).get(method)
    if not op:
        return None, None
    for _ct, v in op.get("requestBody", {}).get("content", {}).items():
        return v.get("schema", {}), op.get("summary", "")
    return None, op.get("summary", "")


def dump(name, depth=0, maxd=2, seen=None):
    seen = seen or set()
    if depth > maxd or name in seen:
        return
    seen.add(name)
    sch = SCHEMAS.get(name)
    if not sch:
        return
    props = sch.get("properties") or {}
    if not props:
        print(f"{'  ' * depth}({name}: type={sch.get('type')}, enum={sch.get('enum')})")
        return
    for key, val in props.items():
        typ = val.get("type", "")
        desc = (val.get("description") or "").strip().replace("\n", " ")[:75]
        enum = val.get("enum")
        rn = ref_name(val)
        if rn:
            print(f"{'  ' * depth}- {key} <{rn}>  {desc}")
            dump(rn, depth + 1, maxd, seen)
        elif typ == "array":
            it = val.get("items", {})
            irn = ref_name(it)
            if irn:
                print(f"{'  ' * depth}- {key}[] <{irn}>  {desc}")
                dump(irn, depth + 1, maxd, seen)
            else:
                en = f" 枚举={it.get('enum')}" if it.get("enum") else ""
                print(f"{'  ' * depth}- {key}[] {it.get('type')}{en}  {desc}")
        else:
            en = f" 枚举={enum}" if enum else ""
            print(f"{'  ' * depth}- {key}: {typ}{en}  {desc}")


TARGETS = [
    ("[1] 项目级 v1", "/v1/selfProduct/add"),
    ("[1] 项目级 v3", "/v3/shop/product/selfBuildActivity"),
    ("[2] 场次级 v1", "/v1/selfProduct/events/add"),
    ("[2] 场次级 v3", "/v3/shop/product/selfBuildActivity/events"),
    ("[3] 票档级 v1", "/v1/selfProduct/tickets/add"),
    ("[3] 票档级 v3", "/v3/shop/product/{productId}/selfBuild"),
    ("[3b] SKU级 v3", "/v3/shop/product/{productId}/selfBuild/sku/{skuId}"),
    ("[4] 年龄限购", "/v1/selfProduct/tickets/ageLimit"),
]

for label, api in TARGETS:
    sch, summ = get_body(api)
    print("\n" + "=" * 78)
    print(f"{label}   {api}")
    print(f"summary: {summ}")
    print("=" * 78)
    if not sch:
        print("  (无请求体)")
        continue
    if ref_name(sch):
        print(f"schema <{ref_name(sch)}>")
        dump(ref_name(sch))
    else:
        print("  内联:", json.dumps(sch, ensure_ascii=False)[:700])
