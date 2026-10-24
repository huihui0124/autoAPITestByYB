"""第四轮：找字典/枚举的权威来源（交付方式、实名类型等数值口径）。"""
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
print("[A] 字典/枚举类接口")
print("=" * 84)
for p in sorted(PATHS):
    if re.search(r"(dict|enum|/code|/type|/config|/option)", p, re.I):
        for m, op in PATHS[p].items():
            print(f"  {m.upper():6} {p:62} {','.join(op.get('tags') or [])}")

print("\n" + "=" * 84)
print("[B] 所有 delivery 相关字段（找带 Name 的伴生字段）")
print("=" * 84)
for sname, sch in SCHEMAS.items():
    for fname, val in (sch.get("properties") or {}).items():
        if "deliver" in fname.lower():
            print(f"  {sname}.{fname}: {val.get('type','?')} "
                  f"枚举={val.get('enum')} {(val.get('description') or '')[:50]}")

print("\n" + "=" * 84)
print("[C] 带枚举定义的字段（全量，看哪些模型给了数值口径）")
print("=" * 84)
cnt = 0
for sname, sch in SCHEMAS.items():
    for fname, val in (sch.get("properties") or {}).items():
        e = val.get("enum")
        if e and not all(isinstance(x, str) for x in e):
            cnt += 1
            print(f"  {sname}.{fname} = {e}")
        elif e and all(isinstance(x, str) for x in e):
            print(f"  {sname}.{fname} = {e}")
print(f"  （共 {cnt} 个数值枚举）")

print("\n" + "=" * 84)
print("[D] 含 realName/needRealName 的模型 + 可能的取值口径")
print("=" * 84)
for sname, sch in SCHEMAS.items():
    for fname, val in (sch.get("properties") or {}).items():
        if re.search(r"realname", fname, re.I):
            print(f"  {sname}.{fname}: {val.get('type','?')} 枚举={val.get('enum')} "
                  f"{(val.get('description') or '')[:60]}")
