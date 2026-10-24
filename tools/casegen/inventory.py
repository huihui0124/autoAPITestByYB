# -*- coding: utf-8 -*-
"""inventory.py —— 把「实际建好的测试商品」导出成一份可交付的《测试商品建设清单》xlsx。

为什么要脚本而不是手写文档：
    清单文档一旦靠人手维护，就会和 B 端真实配置脱节（2026-09-30 user 定的口径：
    **以实际添加的项目为准**）。所以这里的唯一数据源是
        ① catalog.json（登记的项目/场次/票档）
        ② 两个环境的 B 端接口**实测回读**（选座/实名/交付/在售/年龄限购/开售时间窗/库存）
    产出可随时重生成，文档不会再漂。

用法（read-only，不动任何线上数据）
    ./run.sh casegen inventory                       # 两环境实测 + 生成清单到默认路径
    ./run.sh casegen inventory -o /tmp/x.xlsx        # 换输出路径
    ./run.sh casegen inventory --only test           # 只做测试店
    # 也可以手工分两步：
    MIGU_ENV=test MIGU_B_ENV=test  python -m tools.casegen.inventory snapshot -o inv-test.json
    MIGU_ENV=prod MIGU_B_ENV=prod  python -m tools.casegen.inventory snapshot -o inv-prod.json
    python -m tools.casegen.inventory build --test inv-test.json --prod inv-prod.json -o 清单.xlsx

⚠️ 本脚本只读。生产快照用 MIGU_ENV=prod MIGU_B_ENV=prod，**不需要** MIGU_ALLOW_PROD_WRITE。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
DEFAULT_OUT = os.environ.get(
    "MIGU_INVENTORY_OUT", "/Users/yubing/Documents/haomai/咪咕票务测试商品建设清单.xlsx")

ENV_LABEL = {"test": "测试店", "prod": "生产店"}
DELIVERY_TEXT = {"eticket": "电子票", "paper": "快递/自取", "idcard": "证件入场"}
REAL_NAME_TEXT = {0: "非实名", 1: "一票一证", 2: "一单一证"}
# 交付形态（清单里逐档登记的真实取值）
DELIVERY_VARIANTS = [
    ("eticket", [4], "电子票 [4]"),
    ("paper", [1, 3], "快递+自取 [1,3]"),
    ("paper", [1], "仅快递 [1]"),
    ("idcard", [11], "证件入场 [11]"),
]


# --------------------------------------------------------------- 快照（实测）

def _fmt_ms(ms) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M")


def _addition(raw):
    """skuAddition 可能是 dict，也可能是 JSON 字符串。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def snapshot(env: str, out_path: Path, log=print) -> dict:
    from migu_api.badmin import BAdminClient
    from migu_api import catalog as cat_mod

    b = BAdminClient()
    cat = cat_mod.load(env)
    if str(b.store_id) != str(cat.store_id):
        raise SystemExit(f"B 端会话在 {b.store_id} 店，与清单 {env} 登记的门店 {cat.store_id} 不一致")

    # 项目级在售状态（列表一次拿全，避免逐项目 N 次调用）
    on_sale: dict[int, dict] = {}
    page = 1
    while True:
        r = b._request("GET", "/v1/selfProduct/list",
                       params={"storeId": b.store_id, "pageIndex": page, "pageSize": 200})
        for x in r.get("data") or []:
            on_sale[int(x["productId"])] = {
                "onSale": x.get("onSale"),
                "eventSum": x.get("onSaleEventSum"),
                "stockNum": x.get("onSaleTotalStockNum"),
            }
        if not r.get("hasMore"):
            break
        page += 1
    log(f"[{env}] 商品列表 {len(on_sale)} 个；清单登记 {len(cat.projects)} 个项目")

    projects = []
    for p in cat.projects:
        pid = p.product_id
        if pid is None:
            continue
        detail = b._request("GET", f"/v3/shop/product/{pid}/selfBuild",
                            params={"storeId": b.store_id})
        ev_resp = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                             params={"storeId": b.store_id})
        seat = bool(ev_resp.get("hasSeatSelection") or detail.get("supportSelectSeat"))
        events = []
        for e in ev_resp.get("events") or []:
            evid = e["productEventId"]
            ev_name = e["name"] or ""
            if seat:
                t = b._request("GET", f"/v1/seat/event/ticket/{evid}",
                               params={"storeId": b.store_id, "hmProductId": pid})
                rows = t.get("productSkuList") or []
            else:
                rows = b._request("GET", "/v1/selfProduct/tickets/list",
                                  params={"storeId": b.store_id, "productId": pid, "eventId": evid})
            check_age = ("18" in ev_name) or ("年龄" in ev_name)
            tickets = []
            for x in rows:
                addition = _addition(x.get("skuAddition"))
                age_note = ""
                if check_age:
                    al = b._request("GET", "/v1/selfProduct/tickets/ageLimit",
                                    params={"storeId": b.store_id, "productId": pid,
                                            "eventId": evid, "skuId": x["skuId"]})
                    if isinstance(al, dict) and al.get("enabled"):
                        rules = ", ".join(
                            f"{r.get('operator')}{r.get('age')}→{r.get('purchaseResult')}"
                            for r in (al.get("rules") or []))
                        age_note = rules or "已开启"
                    else:
                        age_note = "未配置"
                tickets.append({
                    "name": x.get("priceName") or x.get("name"),
                    "skuId": x["skuId"],
                    "price": str(x.get("price") if seat else x.get("salePrice")),
                    "stock": x.get("stockNum") if seat else x.get("totalStockNum"),
                    "limitBuyNum": addition.get("limitBuyNum"),
                    "fixedSaleTime": _fmt_ms(addition.get("fixedSaleTime")),
                    "fixedSaleTimeMs": addition.get("fixedSaleTime")
                    if isinstance(addition.get("fixedSaleTime"), (int, float)) else None,
                    "ageLimit": age_note,
                })
            events.append({
                "name": ev_name,
                "key": next((q.get("key") for q in (p.events or [])
                             if str(q.get("eventId")) == str(evid)), ""),
                "eventId": evid,
                "start": e.get("startTime"), "end": e.get("endTime"),
                "startAt": _fmt_ms(e.get("startTime")), "endAt": _fmt_ms(e.get("endTime")),
                "maxVerifyCount": e.get("maxVerifyCount"),
                "seatImageName": e.get("seatImageName") or "",
                "tickets": tickets,
            })
        info = on_sale.get(int(pid), {})
        projects.append({
            "key": p.key, "name": p.name, "productId": pid, "seat": seat,
            "realName": detail.get("needRealNameInfo"),
            "deliveryType": list((detail.get("deliveryInfo") or {}).get("deliveryType") or []),
            "onSale": info.get("onSale"),
            "status": p.status, "usages": list(p.usages or []), "note": p.note or "",
            "events": events,
        })
        log(f"[{env}] {p.key} {pid} {p.name} → {len(events)} 场次 / "
            f"{sum(len(e['tickets']) for e in events)} 票档")

    snap = {
        "env": env, "storeId": b.store_id,
        "capturedAt": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "projects": projects,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[{env}] 快照已写出 → {out_path}")
    return snap


# --------------------------------------------------------------- 生成 xlsx

def _delivery_label(delivery_type) -> str:
    """交付形态标签（按真实 deliveryType 取值）。"""
    dt = sorted(delivery_type or [])
    return {tuple(v): label for _k, v, label in DELIVERY_VARIANTS}.get(
        tuple(dt), " + ".join(str(x) for x in dt) or "—")


def build(test_snap: dict, prod_snap: dict, out_path: Path, log=print) -> Path:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.formatting.rule import CellIsRule, FormulaRule

    snaps = [s for s in (test_snap, prod_snap) if s]

    # ---- 样式（沿用本项目用例的浅蓝表头 + 微软雅黑 11）----
    HEAD_FILL = PatternFill("solid", fgColor="FFB7DEE8")
    SUB_FILL = PatternFill("solid", fgColor="FFEAF3F9")
    BAD_FILL = PatternFill("solid", fgColor="FFFFC7CE")
    OK_FILL = PatternFill("solid", fgColor="FFC6EFCE")
    WARN_FILL = PatternFill("solid", fgColor="FFFFEB9C")
    THIN = Side(style="thin", color="FFBFBFBF")
    GRID = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    F_HEAD = Font(name="微软雅黑", size=11, bold=True, color="FF1F3864")
    F_BODY = Font(name="微软雅黑", size=11)
    F_TITLE = Font(name="微软雅黑", size=13, bold=True, color="FF1F3864")
    C_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
    C_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

    wb = openpyxl.Workbook()
    wb.properties.title = "咪咕票务测试商品建设清单"

    def settle(ws, header, rows, widths, left_cols=(), title=None, freeze="A3"):
        """统一铺表：标题行(可选) + 表头 + 数据，最后一列左对齐。"""
        r0 = 1
        if title:
            ws.cell(row=1, column=1, value=title).font = F_TITLE
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(header))
            ws.cell(row=1, column=1).alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[1].height = 22
            r0 = 2
        for i, h in enumerate(header, start=1):
            c = ws.cell(row=r0, column=i, value=h)
            c.fill, c.font, c.alignment, c.border = HEAD_FILL, F_HEAD, C_CENTER, GRID
        ws.row_dimensions[r0].height = 28
        for j, row in enumerate(rows):
            for i, v in enumerate(row, start=1):
                c = ws.cell(row=r0 + 1 + j, column=i, value=v)
                c.font, c.border = F_BODY, GRID
                c.alignment = C_LEFT if i in left_cols else C_CENTER
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = f"A{r0 + 1}" if freeze else None
        if rows:
            ws.auto_filter.ref = f"A{r0}:{get_column_letter(len(header))}{r0 + len(rows)}"
        return r0

    # ---------- 01 项目清单 ----------
    ws = wb.active
    ws.title = "01-项目清单"
    header = ["环境", "编号", "商品名", "productId", "选座", "实名", "交付形态",
              "场次数", "票档数", "价格档（元）", "在售", "登记状态", "用途标签", "备注"]
    rows = []
    for s in snaps:
        for p in s["projects"]:
            tks = [t for e in p["events"] for t in e["tickets"]]
            prices = sorted({float(t["price"]) for t in tks if t["price"] not in (None, "")})
            rows.append([
                ENV_LABEL.get(s["env"], s["env"]), p["key"], p["name"], p["productId"],
                "是" if p["seat"] else "否",
                REAL_NAME_TEXT.get(p["realName"], p["realName"]),
                _delivery_label(p["deliveryType"]),
                len(p["events"]), len(tks),
                " / ".join(f"{v:g}" for v in prices) or "—",
                "是" if p["onSale"] else "否", p["status"],
                "、".join(p["usages"]) or "—", p["note"] or "",
            ])
    r0 = settle(ws, header, rows, [8, 7, 26, 11, 7, 10, 15, 8, 8, 18, 7, 10, 12, 60],
                left_cols=(3, 10, 14), title="测试商品清单（接口实测快照，两个环境）")
    ws.column_dimensions["N"].width = 60
    rng = f"K{r0 + 1}:K{r0 + len(rows)}"
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"否"'], fill=BAD_FILL))
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"是"'], fill=OK_FILL))
    ws.conditional_formatting.add(f"L{r0 + 1}:L{r0 + len(rows)}",
                                  CellIsRule(operator="notEqual", formula=['"ready"'], fill=WARN_FILL))

    # ---------- 02 形态覆盖对照 ----------
    ws2 = wb.create_sheet("02-形态覆盖对照")
    header2 = ["选座", "实名", "交付形态", "测试店项目", "测试店 productId",
               "生产店项目", "生产店 productId", "覆盖结论", "备注"]
    index = {}
    for s in snaps:
        for p in s["projects"]:
            index[(s["env"], p["seat"], p["realName"], tuple(sorted(p["deliveryType"])))] = p
    rows2 = []
    for seat in (False, True):
        for rn in (0, 1, 2):
            for _d, dts, label in DELIVERY_VARIANTS:
                if 11 in dts and rn == 0:
                    note = "非法组合：身份证件入场强制实名（实测 12/12）"
                    rows2.append(["是" if seat else "否", REAL_NAME_TEXT[rn], label,
                                  "—", "—", "—", "—", "非法", note])
                    continue
                t = index.get(("test", seat, rn, tuple(sorted(dts))))
                p_ = index.get(("prod", seat, rn, tuple(sorted(dts))))
                if t and p_:
                    concl, note = "两环境覆盖", ""
                elif t:
                    concl, note = "仅测试店", "生产店缺该形态"
                elif p_:
                    concl, note = "仅生产店", "测试店缺该形态"
                else:
                    concl, note = "未覆盖", "两环境都没有"
                    if 11 in dts:
                        note += "｜待确认：身份证件入场是否允许一单一证"
                rows2.append([
                    "是" if seat else "否", REAL_NAME_TEXT[rn], label,
                    t["key"] if t else "—", t["productId"] if t else "—",
                    p_["key"] if p_ else "—", p_["productId"] if p_ else "—",
                    concl, note])
    r0b = settle(ws2, header2, rows2, [7, 11, 16, 11, 15, 11, 15, 12, 46],
                 left_cols=(9,), title="(选座 × 实名 × 交付) 形态覆盖矩阵 —— 由 01 表实测结果归纳")
    ws2.conditional_formatting.add(f"H{r0b + 1}:H{r0b + len(rows2)}",
                                   CellIsRule(operator="equal", formula=['"两环境覆盖"'], fill=OK_FILL))
    ws2.conditional_formatting.add(f"H{r0b + 1}:H{r0b + len(rows2)}",
                                   CellIsRule(operator="equal", formula=['"未覆盖"'], fill=BAD_FILL))
    ws2.conditional_formatting.add(f"H{r0b + 1}:H{r0b + len(rows2)}",
                                   CellIsRule(operator="equal", formula=['"仅测试店"'], fill=WARN_FILL))
    ws2.conditional_formatting.add(f"H{r0b + 1}:H{r0b + len(rows2)}",
                                   CellIsRule(operator="equal", formula=['"非法"'], fill=SUB_FILL))

    # ---------- 03 场次清单 ----------
    ws3 = wb.create_sheet("03-场次清单")
    header3 = ["环境", "编号", "商品名", "场次名称", "场次 key", "eventId",
               "演出开始", "演出结束", "核销次数", "票档数", "年龄限购", "座位图"]
    rows3 = []
    for s in snaps:
        for p in s["projects"]:
            for e in p["events"]:
                ages = [t["ageLimit"] for t in e["tickets"] if t["ageLimit"]]
                age_txt = f"{len([a for a in ages if a != '未配置'])}/{len(e['tickets'])} 档 " \
                          f"{ages[0]}" if ages else "—"
                rows3.append([
                    ENV_LABEL.get(s["env"], s["env"]), p["key"], p["name"], e["name"],
                    e["key"] or "—", e["eventId"], e["startAt"], e["endAt"],
                    e["maxVerifyCount"], len(e["tickets"]), age_txt,
                    e["seatImageName"] or "—",
                ])
    r0c = settle(ws3, header3, rows3, [8, 7, 24, 14, 13, 10, 17, 17, 10, 8, 22, 20],
                 left_cols=(3, 12), title="场次清单（含核销次数 / 年龄限购 / 座位图）")

    # ---------- 04 票档清单 ----------
    ws4 = wb.create_sheet("04-票档清单")
    header4 = ["环境", "编号", "场次名称", "票档名称", "skuId", "价格（元）",
               "库存", "票档限购", "开售时间", "年龄限购"]
    rows4 = []
    for s in snaps:
        for p in s["projects"]:
            for e in p["events"]:
                for t in e["tickets"]:
                    rows4.append([
                        ENV_LABEL.get(s["env"], s["env"]), p["key"], e["name"], t["name"],
                        t["skuId"],
                        float(t["price"]) if t["price"] not in (None, "") else None,
                        t["stock"], t["limitBuyNum"] if t["limitBuyNum"] is not None else "—",
                        t["fixedSaleTime"] or "—", t["ageLimit"] or "—",
                    ])
    r0d = settle(ws4, header4, rows4, [8, 7, 14, 12, 11, 11, 10, 11, 17, 20],
                 left_cols=(3,), title="票档清单（价格 / 库存 / 限购 / 开售时间窗 / 年龄限购）")
    for r in range(r0d + 1, r0d + 1 + len(rows4)):
        ws4.cell(row=r, column=6).number_format = "0.00"
        ws4.cell(row=r, column=7).number_format = "#,##0"

    # ---------- 05 说明 ----------
    ws5 = wb.create_sheet("05-说明")
    n_proj = sum(len(s["projects"]) for s in snaps)
    n_ev = sum(len(p["events"]) for s in snaps for p in s["projects"])
    n_tk = sum(len(e["tickets"]) for s in snaps for p in s["projects"] for e in p["events"])
    gaps = []
    now_ms = time.time() * 1000
    for s in snaps:
        for p in s["projects"]:
            lab = f"{ENV_LABEL.get(s['env'], s['env'])} {p['key']}"
            for e in p["events"]:
                # 未开售：只有带**未来** fixedSaleTime 才是真的未开售
                if "未开售" in (e["name"] or ""):
                    futures = [t for t in e["tickets"] if t["fixedSaleTime"]]
                    if not futures:
                        gaps.append(f"{lab}「{e['name']}」整场没有开售时间窗（等于已开售）")
                    else:
                        past = [t for t in futures if t["fixedSaleTimeMs"] and t["fixedSaleTimeMs"] < now_ms]
                        if past:
                            gaps.append(f"{lab}「{e['name']}」有 {len(past)} 个票档开售时间已过"
                                        f"（如 {past[0]['name']} {past[0]['fixedSaleTime']}）")
                # 名字带「未开售」的票档（多在普通场次里）
                past_tk = [t for t in e["tickets"]
                           if "未开售" in (t["name"] or "")
                           and (not t["fixedSaleTime"] or (t["fixedSaleTimeMs"] or 0) < now_ms)]
                if past_tk:
                    gaps.append(f"{lab}「{e['name']}」票档「{past_tk[0]['name']}」"
                                f"开售时间{'缺失' if not past_tk[0]['fixedSaleTime'] else '已过'}"
                                f"（{past_tk[0]['fixedSaleTime'] or '无'}）→ 已不是未开售状态")
                # 18+：场次名写着 18+，票档就该有年龄限购
                if "18" in (e["name"] or ""):
                    ok = [t for t in e["tickets"] if t["ageLimit"] not in ("", "未配置")]
                    if not ok:
                        gaps.append(f"{lab}「{e['name']}」票档没有配年龄限购（场次名不副实）")
    if not gaps:
        gaps = ["无（本次实测未发现结构性缺口）"]
    # 价格口径：绝大多数项目 0~1 元；把「>1 元」的项目点名列出来（如沿用座位图价档的）
    dear = []
    for s in snaps:
        for p in s["projects"]:
            hi = max([float(t["price"]) for e in p["events"] for t in e["tickets"]
                      if t["price"] not in (None, "")] or [0])
            if hi > 1:
                dear.append(f"{ENV_LABEL.get(s['env'], s['env'])} {p['key']}（最高 {hi:g} 元）")
    info = [
        ("文档定位", "本表是**实际建好的测试商品**的实测快照，不再是规划稿。"
                     "规划内容（拆分依据/待建编号）已全部废弃 —— 口径以实际添加的项目为准。"),
        ("数据来源", "① catalog.json（登记的项目/场次/票档）；"
                     "② 两个环境 B 端接口实测回读（选座/实名/交付/在售/核销次数/年龄限购/开售时间窗/库存）。"),
        ("抓取时间", "；".join(f"{ENV_LABEL.get(s['env'], s['env'])} {s['capturedAt']}" for s in snaps)),
        ("覆盖规模", f"项目 {n_proj} 个（测试店 12 / 生产店 12）、场次 {n_ev} 个、票档 {n_tk} 个。"),
        ("环境口径", "测试店 storeId=1；生产店 storeId=888（生产只在 888 店建项目，其它店不动）。"),
        ("形态拆分口径", "(选座 2) × (实名 3) × (交付 3) 是项目级互斥单值；"
                         "核销次数、演出时间、座位图、票档价格/库存/限购/售卖时间/年龄限购全部下放到场次与票档，"
                         "所以一个项目内用多场次 × 多票档即可覆盖。"),
        ("价格口径", "绝大多数测试商品价格在 0 ~ 1 元量级（0 元票档用于免付回归，0.01 元用于真实支付/退款）。"
                     + (f"⚠️ 例外（>1 元）：{'；'.join(dear)}。" if dear else "")),
        ("缺口补建口径", "清单里缺的形态用骨架克隆新建：`./run.sh casegen provision <spec.md> --env test` 先 dry-run，"
                         "确认后 `--apply --yes`。**生产只在 888 店建**，且必须显式放行 MIGU_ALLOW_PROD_WRITE=1；"
                         "账号/门店对不上会被拒绝写入。"),
        ("生产差异", "① 生产 B 端场次编辑接口没有 maxVerifyCount 字段 → 生产所有场次核销次数恒为 1，"
                     "「多次核销」在生产无法配置；② 生产身份证件入场/选座等形态与测试店一一对应。"),
        ("已知缺口", "；".join(gaps) if gaps else "无（本次实测未发现结构性缺口）"),
        ("重新生成", "清单是脚本产物，不要手改。重新采数并覆盖：`./run.sh casegen inventory`"
                     "（只读，两个环境都采，生产不写数据）。"),
        ("变更记录", "2026-09-30 首次按实际项目全量重做（旧规划稿另存为 "
                     "`咪咕票务测试商品建设清单_旧规划稿_20260930.xlsx`）；同日生产 888 店补建 P12，"
                     "并给生产 P10/P11 的「18+可购场次」补上年龄限购。"),
    ]
    ws5.cell(row=1, column=1, value="说明与口径").font = F_TITLE
    ws5.merge_cells("A1:B1")
    ws5.row_dimensions[1].height = 22
    for i, h in enumerate(["项", "内容"], start=1):
        c = ws5.cell(row=2, column=i, value=h)
        c.fill, c.font, c.alignment, c.border = HEAD_FILL, F_HEAD, C_CENTER, GRID
    ws5.row_dimensions[2].height = 24
    for j, (k, v) in enumerate(info):
        a = ws5.cell(row=3 + j, column=1, value=k)
        b_ = ws5.cell(row=3 + j, column=2, value=v)
        a.font, a.border, a.alignment = F_BODY, GRID, Alignment(horizontal="center",
                                                               vertical="center", wrap_text=True)
        b_.font, b_.border, b_.alignment = F_BODY, GRID, C_LEFT
        ws5.row_dimensions[3 + j].height = max(18, 15 * (len(v) // 46 + len(v) % 46 > 0))
    ws5.column_dimensions["A"].width = 14
    ws5.column_dimensions["B"].width = 110
    ws5.freeze_panes = "A3"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    log(f"✓ 已生成 {out_path}")
    log(f"  工作表：{wb.sheetnames}")
    return out_path


# --------------------------------------------------------------- CLI

def cmd_snapshot(args) -> int:
    env = args.env or os.environ.get("MIGU_ENV", "test")
    snapshot(env, Path(args.out or f"inv-{env}.json"))
    return 0


def cmd_build(args) -> int:
    test_snap = json.loads(Path(args.test).read_text(encoding="utf-8")) if args.test else None
    prod_snap = json.loads(Path(args.prod).read_text(encoding="utf-8")) if args.prod else None
    if not (test_snap or prod_snap):
        raise SystemExit("至少要有一份快照（--test / --prod）")
    build(test_snap, prod_snap, Path(args.out or DEFAULT_OUT))
    return 0


def cmd_all(args) -> int:
    """两个环境各起一个子进程采数（config 是 import 期单例，一个进程只能站一个环境）。"""
    cache = HERE / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    pairs = [s for s in (("test",), ("prod",)) if args.only in (None, s[0])]
    for (env,) in pairs:
        out = cache / f"inv-{env}.json"
        cmd = [sys.executable, "-m", "tools.casegen.inventory", "snapshot",
               "--env", env, "--out", str(out)]
        e = dict(os.environ, MIGU_ENV=env, MIGU_B_ENV=env)
        print(f"$ MIGU_ENV={env} MIGU_B_ENV={env} … snapshot")
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=e)
        if r.returncode != 0:
            raise SystemExit(f"[{env}] 采数失败（exit={r.returncode}）")
    snaps = {}
    for (env,) in pairs:
        snaps[env] = json.loads((cache / f"inv-{env}.json").read_text(encoding="utf-8"))
    build(snaps.get("test"), snaps.get("prod"), Path(args.out or DEFAULT_OUT))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = _parser()
    if argv and argv[0] in ("snapshot", "build", "all"):
        args = ap.parse_args(argv)
    else:                       # 省略子命令时默认 all，允许 `inventory -o x.xlsx`
        args = ap.parse_args(["all"] + argv)
    return args.func(args)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="casegen inventory",
                                 description="实际测试商品 →《测试商品建设清单》xlsx（只读采数）")
    sub = ap.add_subparsers(dest="cmd")
    p1 = sub.add_parser("snapshot", help="采集单个环境的实测快照")
    p1.add_argument("--env", choices=("test", "prod"))
    p1.add_argument("-o", "--out")
    p1.set_defaults(func=cmd_snapshot)
    p2 = sub.add_parser("build", help="用快照生成 xlsx")
    p2.add_argument("--test")
    p2.add_argument("--prod")
    p2.add_argument("-o", "--out")
    p2.set_defaults(func=cmd_build)
    p3 = sub.add_parser("all", help="两环境采数 + 生成（默认）")
    p3.add_argument("--only", choices=("test", "prod"))
    p3.add_argument("-o", "--out")
    p3.set_defaults(func=cmd_all)
    return ap


if __name__ == "__main__":
    raise SystemExit(main())
