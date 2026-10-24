# -*- coding: utf-8 -*-
"""按接口模型核清「项目 / 场次 / 票档」三层字段归属，以及单值 vs 多值（数组）性质。"""
import json, warnings
warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

c = BAdminClient()
doc = c.session.get(f"{b_env.api_base}/v3/api-docs", timeout=30).json()
S = doc["components"]["schemas"]


def ref(n):
    return (n or {}).get("$ref", "").split("/")[-1]


def dump(name, depth=0, maxd=2, seen=None):
    seen = seen if seen is not None else set()
    if name in seen or depth > maxd:
        return
    seen.add(name)
    s = S.get(name)
    if not s:
        print(f"{'  '*depth}（缺 schema {name}）")
        return
    req = set(s.get("required") or [])
    props = s.get("properties") or {}
    if not props:
        print(f"{'  '*depth}（{name} type={s.get('type')} enum={s.get('enum')}）")
        return
    for k, v in props.items():
        t = v.get("type", "")
        e = v.get("enum")
        d = (v.get("description") or "").replace("\n", " ")[:78]
        r = ref(v)
        star = "*" if k in req else " "
        if r:
            print(f"{'  '*depth}{star} {k} <{r}> {d}")
            dump(r, depth + 1, maxd, seen)
        elif t == "array":
            it = v.get("items", {}) or {}
            ir = ref(it)
            if ir:
                print(f"{'  '*depth}{star} {k}[] <{ir}> {d}")
                dump(ir, depth + 1, maxd, seen)
            else:
                print(f"{'  '*depth}{star} {k}[]:{it.get('type')} enum={it.get('enum')} {d}")
        else:
            print(f"{'  '*depth}{star} {k}:{t} enum={e} {d}")


MODELS = [
    ("【项目级】SelfBuildActivityWebRequest", "SelfBuildActivityWebRequest"),
    ("【场次级】AddEventWebReq", "AddEventWebReq"),
    ("【场次级】Event", "Event"),
    ("【票档级】AddTicketWebReq", "AddTicketWebReq"),
    ("【票档级】SkuSelfAdditionDto", "SkuSelfAdditionDto"),
    ("【场次x票档级】SkuAgeLimitSaveRequest", "SkuAgeLimitSaveRequest"),
]
for label, name in MODELS:
    print(f"\n{'='*84}\n{label}   <{name}>  required={S.get(name, {}).get('required')}\n{'='*84}")
    dump(name, 0, 2)

print(f"\n\n{'#'*84}\n# 全库关键词搜索：字段名/描述命中 → 该字段落在哪个模型的哪一层\n{'#'*84}")
KW = ["realname", "real name", "idcard", "id card", "credential", "certificate", "cert",
      "price", "stock", "verifycount", "verify", "limitbuy", "saletime", "salestart",
      "saleend", "delivery", "seat", "refund", "agelimit", "age"]
LEVEL = {
    "SelfBuildActivityWebRequest": "项目",
    "SelfBuildActivityExtDto": "项目",
    "AddEventWebReq": "场次",
    "Event": "场次",
    "AddTicketWebReq": "票档",
    "SkuSelfAdditionDto": "票档",
    "SkuAgeLimitSaveRequest": "场次x票档",
}
for name, s in S.items():
    props = s.get("properties") or {}
    for k, v in props.items():
        blob = (k + " " + (v.get("description") or "")).lower()
        if any(w in blob for w in KW):
            t = v.get("type", "")
            if t == "array":
                it = v.get("items", {}) or {}
                t = f"array<{ref(it) or it.get('type')}>"
            elif ref(v):
                t = f"<{ref(v)}>"
            enum = v.get("enum") or (v.get("items", {}) or {}).get("enum")
            d = (v.get("description") or "").replace("\n", " ")[:66]
            mark = LEVEL.get(name, "")
            print(f"  [{mark:6}] {name:38} . {k:26} {str(t):26} enum={enum} {d}")
