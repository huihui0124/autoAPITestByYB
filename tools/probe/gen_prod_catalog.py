# -*- coding: utf-8 -*-
"""用测试目录（P01~P11）+ 生产构建注册表，生成 catalog.json 的 prod 区。

映射来源：
  - productId / eventId / skuId：tools/probe/_prod_build_registry.json
  - P01(222945→127)：tools/probe/_p01_prod_events.json（build_prod_p01.py 时代产物，注册表无记录）
  - 匹配规则：场次按「名称」、票档按「名称」（生产是测试的 1:1 复刻，名称一致）
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
CAT = ROOT / "catalog.json"
REG = json.load(open(HERE / "_prod_build_registry.json"))
P01 = json.load(open(HERE / "_p01_prod_events.json"))

catalog = json.load(open(CAT))
test_env = catalog["environments"]["test"]
prod_env = catalog["environments"]["prod"]

src2prod = {k: v.get("pid") for k, v in REG["projects"].items()}
src2prod["222945"] = 127

prod_env["storeId"] = 888
prod_env["note"] = ("生产 888 店测试专用项目（B 端 1:1 复刻自测试店）。\n"
                    "差异：生产无多次核销/年龄限购功能，相关字段仅作标记；"
                    "P01=127 由 build_prod_p01.py 构建，其余由 build_prod_projects.py 构建。\n"
                    f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

for entry in test_env["projects"]:
    key = entry["key"]
    src_pid = str(entry["productId"])
    new = json.loads(json.dumps(entry))
    new["productId"] = src2prod[src_pid]
    new["note"] = f"生产复刻自测试店 {src_pid}（原 note：{entry.get('note', '')}）"

    reg = REG["projects"].get(src_pid, {})
    ev_map = reg.get("events", {})          # srcEventId -> prod productEventId
    sku_map = reg.get("skus", {})           # "srcEventId|票档名" -> prod skuId
    p01 = P01 if src_pid == "222945" else None

    for ev in new.get("events", []):
        src_eid = str(ev["eventId"])
        if p01:
            prod_ev = p01.get(ev["name"])
            if not prod_ev:
                raise SystemExit(f"{key} 场次「{ev['name']}」在生产 127 里找不到（按名匹配）")
            ev["eventId"] = prod_ev["productEventId"]
            for sku in ev.get("skus", []):
                prod_sku = prod_ev["skus"].get(sku["name"])
                if not prod_sku:
                    raise SystemExit(f"{key}「{ev['name']}」票档「{sku['name']}」在生产找不到")
                sku["skuId"] = prod_sku
        else:
            if src_eid not in ev_map:
                raise SystemExit(f"{key} 场次 srcEventId={src_eid} 不在注册表")
            ev["eventId"] = ev_map[src_eid]
            for sku in ev.get("skus", []):
                k = f"{src_eid}|{sku['name']}"
                if k not in sku_map:
                    raise SystemExit(f"{key} 票档 {k} 不在注册表")
                sku["skuId"] = sku_map[k]

    prod_env["projects"] = [p for p in prod_env["projects"] if p["key"] != key]
    prod_env["projects"].append(new)

prod_env["projects"].sort(key=lambda p: p["key"])
catalog["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

shutil.copy(CAT, str(CAT) + ".bak.prodgen")
json.dump(catalog, open(CAT, "w"), ensure_ascii=False, indent=2)
n_ev = sum(len(p.get("events", [])) for p in prod_env["projects"])
n_sku = sum(len(e.get("skus", [])) for p in prod_env["projects"] for e in p.get("events", []))
print(f"prod 区生成：{len(prod_env['projects'])} 项目 / {n_ev} 场次 / {n_sku} 票档")
for p in prod_env["projects"]:
    print(f"  {p['key']} {p['productId']} {p['name']}")
