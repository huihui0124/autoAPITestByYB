# -*- coding: utf-8 -*-
"""临时探针：打印退款 calculate/partial/partialIndex 的完整 requestBody schema。用完可删。"""
import json

import requests
import urllib3

from migu_api.badmin import B_COOKIE_FILE
from migu_api.config import b_env

urllib3.disable_warnings()
s = requests.Session()
s.headers.update({"hm-store-id": b_env.store_id, "Referer": b_env.front_url + "/",
                  "User-Agent": "Mozilla/5.0"})
s.proxies = {"http": None, "https": None}
if b_env.base_url.startswith("https://"):
    s.verify = True
for c in json.loads(B_COOKIE_FILE.read_text(encoding="utf-8")):
    s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

doc = s.get(b_env.api_base + "/v3/api-docs", timeout=25).json()
paths, schemas = doc["paths"], doc["components"]["schemas"]

TARGETS = [
    "/v1/shopTrade/refund/calculate",
    "/v1/shopTrade/refund/partial",
    "/v1/shopTrade/refund/partialIndex",
]

seen = set()


def dump(name, depth=0):
    if name in seen or depth > 4:
        return
    seen.add(name)
    node = schemas.get(name)
    if not node:
        print("  " * depth + f"[{name} 未定义]")
        return
    req = set(node.get("required") or [])
    print("  " * depth + f"### {name}  required={sorted(req)}")
    if node.get("description"):
        print("  " * depth + f"    desc: {node['description']}")
    for k, v in (node.get("properties") or {}).items():
        t = v.get("type")
        ref = v.get("$ref", "").split("/")[-1]
        if t == "array":
            inner = v.get("items") or {}
            iref = inner.get("$ref", "").split("/")[-1]
            t = f"array<{inner.get('type') or iref}>"
            child = iref
        else:
            child = ref
        mark = "*" if k in req else " "
        desc = (v.get("description") or "").replace("\n", " ")[:60]
        print("  " * depth + f"   {mark} {k}: {t or ref}  {desc}")
        if child:
            dump(child, depth + 1)


for p in TARGETS:
    print("=" * 70)
    for method, spec in paths[p].items():
        print(f"\n### {method.upper()} {p}")
        if spec.get("summary"):
            print("  summary:", spec["summary"])
        for ct, c in ((spec.get("requestBody") or {}).get("content") or {}).items():
            sch = c.get("schema") or {}
            ref = sch.get("$ref", "").split("/")[-1]
            print(f"  body({ct}) -> {ref or json.dumps(sch, ensure_ascii=False)[:200]}")
            if ref:
                dump(ref)
        resp = (spec.get("responses") or {}).get("200", {})
        for ct, c in (resp.get("content") or {}).items():
            sch = c.get("schema") or {}
            print("  resp 200 ->", sch.get("$ref", "").split("/")[-1]
                  or json.dumps(sch, ensure_ascii=False)[:200])
            break
