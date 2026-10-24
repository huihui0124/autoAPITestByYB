"""查实「身份证件入场(11)」的两个约束（只读，不写任何数据）：
   ① 是否强制实名 —— 11 的项目 needRealNameInfo 是不是都为非 0
   ② 是否可与选座共存 —— 11 的项目 supportSelectSeat 取值分布
同时对照 [4] 电子票 与 [1]/[3]/[1,3] 纸质票，确认三大类各自的实名/选座分布。
"""
import json
import warnings

warnings.filterwarnings("ignore")
from collections import Counter, defaultdict

from migu_api.badmin import BAdminClient

c = BAdminClient()

# 1) 拉全量商品，按 deliveryType 分组
rows = []
for pi in (1, 2, 3, 4, 5):
    d = c._request("GET", "/v1/selfProduct/list",
                   params={"storeId": c.store_id, "pageIndex": pi, "pageSize": 100})
    r = d.get("data") if isinstance(d, dict) else d
    if isinstance(r, dict):
        r = r.get("data") or r.get("list") or []
    if not r:
        break
    rows += r
    if len(r) < 100:
        break

print(f"商品总数: {len(rows)}")


def group_of(dt):
    dt = sorted(dt or [])
    if dt == [4]:
        return "电子票 [4]"
    if dt == [11]:
        return "身份证件入场 [11]"
    if dt and set(dt) <= {1, 3}:
        return f"纸质票 {dt}"
    return f"其他 {dt}"


by_group = defaultdict(list)
for r in rows:
    by_group[group_of(r.get("deliveryType"))].append(r)

print("\n=== 按交付方式分组 ===")
for g, items in sorted(by_group.items()):
    print(f"  {g:24} {len(items):4} 个")

# 2) 逐个查项目详情，取 needRealNameInfo / supportSelectSeat
print("\n=== 项目级配置分布（读 /v3/shop/product/{id}/selfBuildActivity）===")
stats = defaultdict(Counter)
sample = {}
for g, items in sorted(by_group.items()):
    picked = items[:12]          # 每组抽 12 个，够看分布
    for r in picked:
        pid = r.get("productId")
        try:
            d = c._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity",
                           params={"storeId": c.store_id})
            d = d.get("data") if isinstance(d, dict) and "data" in d else d
        except Exception as e:
            print(f"    {pid} 读取失败: {str(e)[:50]}")
            continue
        rn = d.get("needRealNameInfo")
        seat = d.get("supportSelectSeat")
        stats[g][("实名", rn)] += 1
        stats[g][("选座", seat)] += 1
        # 记录一个非实名的 11 样本（如果存在）
        if g.startswith("身份证件入场") and rn == 0:
            sample.setdefault("11非实名", []).append(pid)

for g in sorted(stats):
    print(f"\n  【{g}】")
    for (dim, val), n in sorted(stats[g].items()):
        print(f"     {dim} = {val!r:8} {n:3} 个")

print("\n=== 关键判定 ===")
w11 = [g for g in stats if g.startswith("身份证件入场")]
if w11:
    s = stats[w11[0]]
    rn_vals = {v: n for (d, v), n in s.items() if d == "实名"}
    seat_vals = {v: n for (d, v), n in s.items() if d == "选座"}
    print(f"  11 的实名取值分布: {rn_vals}   → {'★ 强制实名（无非实名实例）' if 0 not in rn_vals else '存在非实名实例，不强制'}")
    print(f"  11 的选座取值分布: {seat_vals}   → {'可与选座共存' if True in seat_vals else '仅非选座'}")
