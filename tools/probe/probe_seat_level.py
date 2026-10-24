"""读-only：看票档级 supportSelectSeat 是否真能在一项目内分化。

若 283 个商品里存在「同一项目下既有一个 supportSelectSeat=true 的票档、
又有 false 的票档」的实例，说明选座可下放票档 → 选座维度不必拆项目。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from migu_api.badmin import BAdminClient

c = BAdminClient()
SID = c.store_id


def events(pid):
    try:
        d = c._request("GET", "/v1/selfProduct/events/list",
                       params={"storeId": SID, "productId": pid})
    except Exception as e:
        return []
    rows = d.get("data") if isinstance(d, dict) else d
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("list") or []
    return rows or []


def tickets(pid, eid):
    try:
        d = c._request("GET", "/v1/selfProduct/tickets/list",
                       params={"storeId": SID, "productId": pid, "eventId": eid})
    except Exception as e:
        return []
    rows = d.get("data") if isinstance(d, dict) else d
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("list") or []
    return rows or []


# 挑一批不同维度的商品
rows = []
for pi in (1, 2):
    d = c._request("GET", "/v1/selfProduct/list",
                   params={"storeId": SID, "pageIndex": pi, "pageSize": 100})
    r = d.get("data") if isinstance(d, dict) else d
    if isinstance(r, dict):
        r = r.get("data") or r.get("list") or []
    rows += r or []

targets = [r for r in rows if "yb" in (r.get("productName") or "").lower()][:18]
print(f"扫描 {len(targets)} 个 yb 商品\n")

mixed = []
for p in targets:
    pid = p.get("productId")
    proj_seat = (p.get("extData") or {}).get("supportSelectSeat")
    evs = events(pid)
    if not evs:
        continue
    e0 = evs[0].get("eventId") or evs[0].get("id")
    tks = tickets(pid, e0)
    if not tks:
        continue
    flags = set()
    detail = []
    for t in tks:
        sk = t.get("skuAddition")
        if isinstance(sk, str):
            try:
                sk = json.loads(sk) if sk else {}
            except Exception:
                sk = {}
        sk = sk or {}
        f = sk.get("supportSelectSeat")
        flags.add(f)
        detail.append(f"{t.get('name') or t.get('skuName')}:{f}")
    flag = "混合" if len(flags) > 1 else (list(flags)[0] if flags else "-")
    mark = "  ★混合" if len(flags) > 1 else ""
    print(f"  {pid} {str(p.get('productName'))[:26]:28} 项目级选座={str(proj_seat):5} "
          f"场次{len(evs)} 票档{len(tks)} 票档级选座={flag}{mark}")
    if len(flags) > 1:
        mixed.append((pid, detail))

print(f"\n=== 存在混合选座票档的项目：{len(mixed)} 个 ===")
for pid, det in mixed[:5]:
    print(f"  {pid}: {det[:6]}")
if not mixed:
    print("  无 —— 现有商品里未见「同项目内选座/非选座票档并存」，")
    print("  说明票档级该字段或为随项目继承、或业务上不这么用；")
    print("  要坐实需要一次写入实验（改某个票档的 skuAddition 再恢复）。")
