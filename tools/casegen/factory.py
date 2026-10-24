# -*- coding: utf-8 -*-
"""
factory.py —— 用例要的测试项目**不存在时，自动在 B 端建好并登记进商品清单**

三种情况，三种处理（这是本模块的核心判断，不要一律去建项目）
    ① 清单里有现成的      → 直接复用（resolve 已给出目标）
    ② 清单里有同形态项目，只是**没打用途标签** → 补登记标签即可，不建新项目
       （例：P04 是"非选座-一票一证-电子票"，但它的 0 元档没打 free_fixture 标签）
    ③ 清单里**根本没有这种形态**（选座/实名/交付方式的组合缺失）→ 才去建项目

建项目怎么建
    同店内**克隆一个"形态最接近"的既有项目**做骨架（项目详情/场次结构/退款规则都在里面），
    再按目标形态覆盖三处关键字段：
        supportSelectSeat   选座/非选座
        needRealNameInfo    0 非实名 / 1 一票一证 / 2 一单一证
        deliveryInfo        电子票[4] / 快递·自取[1,3] / 身份证件入场[11]
    座位图（选座项目）：从 donor 克隆座位区域并按新场次/新 sku 重绑（seat/save 同构 payload）。
    最后：上架（onSale/true）→ 登记进清单（走 catalog_ctl add，保持"清单只有一个写入方"）。

安全闸门（照抄项目里既有的约定，不另发明）
    · 默认只在 **test** 环境建；要动 prod 必须 MIGU_ALLOW_PROD_WRITE=1 且 --apply --yes
    · 任何写操作前先打印"将要做什么"（dry-run 是默认行为）
    · 幂等：tools/casegen/_created_projects.json 记账，重跑不会重复建

用法
    ./run.sh casegen provision specs/xxx.md                 # 只报告缺什么（dry-run）
    ./run.sh casegen provision specs/xxx.md --apply          # 真的建 + 登记（test 环境）
"""
from __future__ import annotations

import json
import os
import random
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")

from migu_api import catalog
from migu_api.badmin import BAdminClient
from migu_api.config import b_env

from .resolve import Requirement, parse_requirement

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
LEDGER = HERE / "_created_projects.json"

# 「形态」维度 = 必须靠建项目才能补的；「标签」维度 = 补登记就能解决的
FORM_KEYS = ("seat", "real_name", "delivery", "event", "sku", "project")
TAG_KEYS = ("usage", "min_verify", "price")

_DELIVERY_TYPES = {"eticket": [4], "paper": [1, 3], "idcard": [11]}
_REAL_NAME_TEXT = {0: "非实名", 1: "一票一证", 2: "一单一证"}
_DELIVERY_TEXT = {"eticket": "电子票", "paper": "快递/自取", "idcard": "证件入场"}


class FactoryError(AssertionError):
    """建项目过程中不可继续的错误（能力未上线 / 缺前置 / 触了安全闸门）。"""


# ------------------------------------------------------------------ 蓝图

@dataclass
class Blueprint:
    """「需要建一个什么项目」的完整描述。"""

    env: str
    requirement: Requirement
    seat: bool
    real_name: int
    delivery: str
    key: str = ""
    name: str = ""
    donor_key: str = ""            # 克隆骨架来源
    events: list[str] = field(default_factory=list)
    note: str = ""

    def describe(self) -> str:
        return (f"[{self.env}] 新建项目 {self.key}「{self.name}」"
                f"（{'选座' if self.seat else '非选座'} / "
                f"{_REAL_NAME_TEXT.get(self.real_name, self.real_name)} / "
                f"{_DELIVERY_TEXT.get(self.delivery, self.delivery)}）"
                f"，骨架克隆自 {self.donor_key}，场次 {len(self.events)} 个"
                f"（{', '.join(self.events) or '—'}）")


@dataclass
class Decision:
    """一条环境要求的处置结论。"""

    raw: str
    action: str            # reuse / relabel / create / unsupported
    detail: str = ""
    blueprint: Blueprint | None = None
    cases: list[str] = field(default_factory=list)
    target_label: str = ""


def _next_key(env: str) -> str:
    """给出下一个可用的项目编号（P12、P13 …）。"""
    cat = catalog.load(env)
    used = {p.key for p in cat.projects}
    for i in range(1, 200):
        k = f"P{i:02d}"
        if k not in used:
            return k
    raise FactoryError("项目编号已用尽")


def _pick_donor(env: str, seat: bool) -> str:
    """挑一个同店、同"选座形态"的既有项目当骨架（优先 ready 的主基线）。"""
    cat = catalog.load(env)
    cands = [p for p in cat.projects
             if p.status == "ready" and bool(p.seat) == bool(seat) and p.product_id]
    if not cands:
        cands = [p for p in cat.projects if p.product_id and bool(p.seat) == bool(seat)]
    if not cands:
        raise FactoryError(f"[{env}] 清单里没有可当骨架的{'选座' if seat else '非选座'}项目")
    # 优先堆叠了最多能力的项目（场次多、票档多）
    cands.sort(key=lambda p: (-len(p.events), p.key))
    return cands[0].key


def diagnose(env: str, raw: str) -> Decision:
    """判断一条环境要求该怎么满足：复用 / 补标签 / 新建 / 建不了。"""
    from .resolve import resolve_requirements
    res = resolve_requirements(raw, env)
    if res.ok:
        return Decision(raw, "reuse", detail=res.best.label, target_label=res.best.label)

    req = res.requirement
    form = {k: v for k, v in req.kwargs.items() if k in FORM_KEYS}
    if req.exact_max_verify is not None:
        form["max_verify"] = req.exact_max_verify

    # 放松"标签类"条件后若能命中 → 缺的是登记，不是项目
    if any(k in req.kwargs for k in TAG_KEYS):
        cat = catalog.load(env)
        loose = cat.find(**form) if form else []
        if loose:
            return Decision(
                raw, "relabel",
                detail=f"已有同形态项目 {loose[0].project_key}（{loose[0].project_name}），"
                       f"但用途标签不匹配：需要 {sorted(req.kwargs.get('usage') or [])}"
                       f"，实际 {sorted(loose[0].usages)}",
                target_label=loose[0].label)

    # 形态缺失 → 需要建项目
    seat = bool(req.kwargs.get("seat", False))
    real_name = int(req.kwargs.get("real_name", 0) or 0)
    delivery = str(req.kwargs.get("delivery", "eticket") or "eticket")
    if delivery not in _DELIVERY_TYPES:
        return Decision(raw, "unsupported",
                        detail=f"交付方式「{delivery}」不是 eticket/paper/idcard 之一")
    try:
        donor = _pick_donor(env, seat)
    except FactoryError as e:
        return Decision(raw, "unsupported", detail=str(e))

    key = _next_key(env)
    name = (f"{'选座' if seat else '非选座'}-"
            f"{_REAL_NAME_TEXT.get(real_name, f'realName{real_name}')}-"
            f"{_DELIVERY_TEXT.get(delivery, delivery)}-yb")
    donor_proj = next(p for p in catalog.load(env).projects if p.key == donor)
    events = [e.get("name") or e.get("key") for e in donor_proj.events]
    bp = Blueprint(env=env, requirement=req, seat=seat, real_name=real_name,
                   delivery=delivery, key=key, name=name, donor_key=donor, events=events,
                   note=f"由用例环境要求自动推导：{raw}")
    return Decision(raw, "create",
                    detail=f"清单里没有「{bp.name}」这种形态 → 需要新建 {key}"
                           f"（骨架 {donor}）",
                    blueprint=bp)


# ------------------------------------------------------------------ 建项目

def _seat_id_factory():
    base = int(time.time()) + int(1e5 * random.random())
    n = [0]

    def nxt():
        n[0] += 1
        return base + n[0]
    return nxt


def _build_area_list(seat_areas, new_seat_id):
    """克隆座位区域：保留座位坐标/颜色/票档名，座位 ID 全部换新。"""
    out = []
    for area in seat_areas or []:
        a = {k: v for k, v in area.items() if k != "areaId"}
        seats = []
        for st in area.get("seatList") or []:
            seats.append({
                "seatId": new_seat_id(),
                "sectionName": st.get("sectionName", ""),
                "seatName": st["seatName"], "rowName": st["rowName"],
                "x": st["x"], "y": st["y"], "saleStatus": 0,
                "color": st["color"],
                "ticket": {"priceName": st["ticket"]["priceName"],
                           "price": st["ticket"]["price"]},
                "enable": 1, "remarks": "",
            })
        a["seatList"] = seats
        out.append(a)
    return out


def _build_ticket_price_list(product_sku_list):
    out = []
    for e in product_sku_list or []:
        if e.get("status") == 0:
            continue
        addition = e.get("skuAddition") or {}
        # ★ fixedSaleTime / fixedSaleEndTime 必须一起搬：这是「未开售场次」的**唯一载体**
        #   （seat/save 的票档没有别的字段能表达开售时间窗）。漏掉它 → 新项目的
        #   「未开售场次」其实已经在售，「未到开售时间下单被拒」这条负向用例就覆盖不了，
        #   `catalog drift --deep` 会报「未开售时间窗缺失」。
        out.append({
            "name": e["priceName"], "salePrice": +e["price"],
            "skuAddition": {k: v for k, v in
                            (("type", addition.get("type")),
                             ("bindId", e.get("bindId") or addition.get("bindId")),
                             ("limitBuyNum", addition.get("limitBuyNum")),
                             ("bindSkuCount", addition.get("bindSkuCount")),
                             ("fixedSaleTime", addition.get("fixedSaleTime")),
                             ("fixedSaleEndTime", addition.get("fixedSaleEndTime")))
                            if v is not None},
        })
    return out


# 「未开售场次」的名字特征（骨架里这场的票档本该带未来的 fixedSaleTime）
_NOT_STARTED_HINTS = ("未开售", "未到开售", "not_started", "notstarted")


def _is_not_started(event_name: str) -> bool:
    n = (event_name or "").lower().replace(" ", "").replace("_", "")
    return any(h.lower().replace(" ", "").replace("_", "") in n
               for h in _NOT_STARTED_HINTS)


def _not_started_window_ms(event_name: str, additions, days: int = 180) -> int | None:
    """「未开售场次」+ 现有票档**没有**未来 fixedSaleTime 时 → 返回要补的时间戳，否则 None。"""
    if not _is_not_started(event_name):
        return None
    now_ms = int(time.time() * 1000)
    if any(isinstance(a.get("fixedSaleTime"), (int, float)) and a["fixedSaleTime"] > now_ms
           for a in (additions or [])):
        return None                             # 骨架带了未来窗 → 不干预
    return now_ms + days * 24 * 3600 * 1000


def _ensure_not_started_window(price_list, event_name: str, log=print):
    """骨架没带开售时间窗时，给「未开售场次」的票档兜底补一个**未来**时间窗。

    为什么需要兜底：`seat/save` 的票档里 fixedSaleTime 是「未开售」的**唯一载体**
    （票档/场次的其它字段都表达不了开售时间）。而骨架本身可能就没有这个窗 ——
    2026-09-30 实测**生产 888 店的 P10(181) 未开售场次 5 个票档 fixedSaleTime 全为 None**，
    于是 1:1 克隆出来的「未开售场次」其实已经开售：
        · `catalog drift` 报「未开售时间窗缺失」（生产巡检当时正是报的 P10 这条）；
        · 「未到开售时间不可下单」这条负向用例在生产无从覆盖。
    测试店的骨架（P10=222963）带了窗（≈2028 年），所以本兜底在测试环境是空操作。
    """
    if not price_list:
        return price_list
    when = _not_started_window_ms(event_name, [x["skuAddition"] for x in price_list])
    if when is None:
        return price_list
    for x in price_list:
        x["skuAddition"]["fixedSaleTime"] = when
    log(f"        骨架的「{event_name}」没有开售时间窗 → 兜底补一个未来时间"
        f"（{time.strftime('%Y-%m-%d', time.localtime(when / 1000))}）")
    return price_list


def _is_age18(event_name: str) -> bool:
    return "18+" in (event_name or "") or "年龄" in (event_name or "")



def _clone_age_limits(b, donor_pid: int, new_pid: int, pairs, log=print) -> int:
    """把骨架项目的「年龄限购」逐「场次 × 票档」搬到新项目。

    为什么必须单独搬：项目/场次/票档的克隆不包含年龄限购 ——
    不搬的话，新项目的「18+可购场次」就是个**名字对、行为不对**的空壳，
    拿它跑 18+ 用例会得到假结果（实测骨架 P10 的 18+ 场次 5 个票档全部 GTE 18 ALLOW）。

    pairs: [{"donorEventId":…, "newEventId":…,
             "donorSkus": {票档名: skuId}, "newSkus": {票档名: skuId}}]
    """
    n = 0
    for p in pairs:
        copied = 0
        for name, don_sku in (p.get("donorSkus") or {}).items():
            new_sku = (p.get("newSkus") or {}).get(name)
            if new_sku is None:
                continue
            try:
                al = b._request("GET", "/v1/selfProduct/tickets/ageLimit",
                                params={"storeId": b.store_id, "productId": donor_pid,
                                        "eventId": p["donorEventId"], "skuId": don_sku})
            except Exception:                       # 没配过年龄限购的商品多数会报错
                continue
            if not (isinstance(al, dict) and al.get("enabled")):
                continue
            rules = [{k: r[k] for k in ("operator", "age", "purchaseResult") if k in r}
                     for r in (al.get("rules") or [])]
            if _put_age_limit(b, new_pid, p["newEventId"], new_sku,
                              al.get("conditionRelation") or "AND", rules, log):
                copied += 1
        n += copied
        # 骨架这场压根没配年龄限购（实测生产 888 店的骨架 P10(181) 就是这种情况）→
        # 按场次名兜底写一条默认规则，否则新项目的「18+可购场次」是**名字对、行为不对**的空壳。
        if copied == 0 and p.get("age18"):
            for _name, new_sku in (p.get("newSkus") or {}).items():
                if _put_age_limit(b, new_pid, p["newEventId"], new_sku, "AND",
                                  [{"operator": "GTE", "age": 18, "purchaseResult": "ALLOW"}], log):
                    n += 1
            log(f"        骨架的「{p.get('eventName')}」没有年龄限购配置 → 兜底写 GTE 18 ALLOW")
    if n:
        log(f"        年龄限购已写入 {n} 条")
    return n


def _put_age_limit(b, pid: int, event_id, sku_id, relation: str, rules, log=print) -> bool:
    """写一条年龄限购；环境不支持时**不抛**（只告警），避免半途炸掉整个建项目流程。"""
    try:
        b._request("POST", "/v1/selfProduct/tickets/ageLimit",
                   json={"storeId": int(b.store_id), "productId": pid,
                         "eventId": event_id, "skuId": sku_id,
                         "ageLimitConfig": {"enabled": True,
                                            "conditionRelation": relation,
                                            "rules": rules}})
        return True
    except Exception as e:                          # 生产可能未发布该能力 → 降级告警
        log(f"        ⚠️ 年龄限购写入失败（eventId={event_id} skuId={sku_id}）：{str(e)[:160]}")
        return False


def load_ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}


def save_ledger(d: dict) -> None:
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def create_project(bp: Blueprint, apply: bool = False, log=print) -> dict:
    """按蓝图建项目。apply=False 时只打印计划（默认，安全）。"""
    log(f"\n>>> {bp.describe()}")
    if not apply:
        log("    （dry-run：未做任何写操作；确认无误后加 --apply）")
        return {"dry_run": True, "blueprint": bp.__dict__}

    _guard_write_env(bp.env)

    # 账本按「环境:编号」记账 —— 同一个 P12 在 test 与 prod 是两个不同的商品，
    # 只用编号当键会让第二环境被"已建过，跳过"误挡（test 建完 P12 后，prod 永远建不出来）。
    led_key = f"{bp.env}:{bp.key}"
    led = load_ledger()
    prev = led.get(led_key) or (led.get(bp.key)
                                if (led.get(bp.key) or {}).get("env") == bp.env else None)
    if prev and prev.get("productId"):
        log(f"    账本显示 {led_key} 已建过 productId={prev['productId']}，跳过")
        return prev

    b = BAdminClient()
    if str(b.store_id) != str(catalog.load(bp.env).store_id):
        raise FactoryError(f"B 端会话在 {b.store_id} 店，与清单登记的门店不一致，拒绝写入")

    donor = next(p for p in catalog.load(bp.env).projects if p.key == bp.donor_key)
    if donor.product_id is None:
        raise FactoryError(f"骨架项目 {bp.donor_key} 没有 productId")

    # 1) 项目详情
    detail = b._request("GET", f"/v3/shop/product/{donor.product_id}/selfBuildActivity",
                        params={"storeId": b.store_id})
    body = dict(detail)
    for k in ("id", "onSale", "purchaseTimeStatus"):
        body.pop(k, None)
    body["storeId"] = int(b.store_id)
    body["activityName"] = bp.name
    body["supportSelectSeat"] = bp.seat
    body["needRealNameInfo"] = bp.real_name
    dt = list(_DELIVERY_TYPES[bp.delivery])
    old = dict(body.get("deliveryInfo") or {})
    delivery_info = {"deliveryType": dt, "deliveryOrigin": old.get("deliveryOrigin", "HAOMAI")}
    if 1 in dt and old.get("postageTemplateId"):
        delivery_info["postageTemplateId"] = old["postageTemplateId"]
    if 3 in dt and old.get("selfPickUpAddressId"):
        delivery_info["selfPickUpAddressId"] = old["selfPickUpAddressId"]
    body["deliveryInfo"] = delivery_info
    if body.get("goodsGroupInfo") is None:
        body["goodsGroupInfo"] = {"groupIds": []}
    if body.get("goodsTagInfo") is None:
        body["goodsTagInfo"] = {"tagIds": []}

    r1 = b._request("POST", "/v1/selfProduct/add", json=body)
    pid = r1 if isinstance(r1, int) else int(str(r1).strip())
    log(f"  [1/6] 项目已建 productId={pid}")

    # 2) 场次（沿用骨架项目的场次名与时长；时间平移到"今天之后"避免一建就已结束）
    donor_events = b._request("GET",
                              f"/v3/shop/product/{donor.product_id}/selfBuildActivity/events",
                              params={"storeId": b.store_id})
    drows = donor_events.get("events") or donor_events.get("data") or []
    if not drows:
        raise FactoryError(f"骨架项目 {bp.donor_key} 没有场次可克隆")
    day = 3600 * 1000 * 24
    base = int(time.time() * 1000) + 3 * day
    payload_events = []
    for i, e in enumerate(drows):
        payload_events.append({
            "eventName": e["name"],
            "startTime": _ms2str(base + i * day),
            "endTime": _ms2str(base + i * day + 3 * 3600 * 1000),
        })
    b._request("POST", "/v1/selfProduct/events/add",
               json={"storeId": int(b.store_id), "productId": pid, "events": payload_events})
    new_events = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                            params={"storeId": b.store_id})
    nrows = new_events.get("events") or new_events.get("data") or []
    name2new = {e["name"]: e for e in nrows}
    log(f"  [2/6] 场次 {len(nrows)} 个：{list(name2new)}")

    # 3) 票档：非选座走 tickets/add；选座走 seat/save（票档由座位图导入产生）
    sku_map: dict[str, dict] = {}      # 场次名 → {票档名: {"skuId":…, "price":…}}
    donor_sku_map: dict[str, dict] = {}   # 场次名 → 骨架该场次的 {票档名: skuId}
    if bp.seat:
        # ★ 必须**一个场次一次 seat/save**。实测：eventIdList 里传多个场次时，平台会把
        #   ticketPriceList 复制到每个场次，且这批 sku 会挂到全部场次下 —— 5 场次 × 5 档
        #   读完每个场次都是同样的 25 个 skuId；而 B 端 UI 逐场次保存出来的结构是
        #   「每场次各自 5 个独立 skuId」（见 P08–P11）。后者才是规范形态，且事后再补
        #   单场次 seat/save 会被平台拒绝（已有座位图 → 500 保存异常），只能重建。
        venue_id = body.get("venue", {}).get("poiValueId")
        for e in drows:
            ev_name = e["name"]
            nev = name2new[ev_name]
            src = b._request("GET", f"/v1/seat/event/{e['productEventId']}",
                             params={"storeId": b.store_id, "hmProductId": donor.product_id})
            price_list = _build_ticket_price_list(src.get("productSkuList"))
            if not price_list:
                raise FactoryError(f"骨架场次「{ev_name}」的座位图里没有票档可克隆")
            price_list = _ensure_not_started_window(price_list, ev_name, log=log)
            donor_sku_map[ev_name] = {x["priceName"]: x["skuId"]
                                      for x in (src.get("productSkuList") or [])}
            body_seat = {
                "eventIdList": [{"spuEventId": nev.get("spuEventId"),
                                 "productEventId": nev["productEventId"],
                                 "eventName": ev_name}],
                "hmProductId": pid,
                "venueId": venue_id,
                "areaList": _build_area_list(src.get("seatAreaList"), _seat_id_factory()),
                "ticketPriceList": price_list,
            }
            resp = b.session.post(f"{b_env.api_base}/v1/seat/save?storeId={b.store_id}",
                                  json=body_seat, timeout=180)
            try:
                rj = resp.json()
            except ValueError:
                rj = {"raw": resp.text[:300]}
            if resp.status_code != 200 or rj.get("code") != 200:
                raise FactoryError(f"座位图保存失败（场次「{ev_name}」）："
                                   f"{json.dumps(rj, ensure_ascii=False)[:300]}")
            time.sleep(1.5)
        # 回读：每个场次应恰好是骨架该场次的票档数（多出来就是又走回了批量保存）
        for e in drows:
            ev_name = e["name"]
            evid = name2new[ev_name]["productEventId"]
            d = b._request("GET", f"/v1/seat/event/ticket/{evid}",
                           params={"storeId": b.store_id, "hmProductId": pid})
            rows = d.get("productSkuList") or []
            sku_map[ev_name] = {x["priceName"]: {"skuId": x["skuId"],
                                                 "price": str(x["price"])} for x in rows}
            if len(rows) != len(donor_sku_map[ev_name]):
                raise FactoryError(
                    f"场次「{ev_name}」票档数 {len(rows)} 与骨架 "
                    f"{len(donor_sku_map[ev_name])} 不一致 —— "
                    f"疑似又变成多场次共享 sku，请检查 seat/save 的 eventIdList")
        log(f"  [3/6] 座位图 + 票档已导入（逐场次）："
            f"{ {k: len(v) for k, v in sku_map.items()} }")
    else:
        for e in drows:
            evid = name2new[e["name"]]["productEventId"]
            cur = b._request("GET", "/v1/selfProduct/tickets/list",
                             params={"storeId": b.store_id, "productId": pid, "eventId": evid})
            cur_names = {t["name"]: t["skuId"] for t in cur}
            src_tickets = b._request("GET", "/v1/selfProduct/tickets/list",
                                     params={"storeId": b.store_id,
                                             "productId": donor.product_id,
                                             "eventId": e["productEventId"]})
            donor_sku_map[e["name"]] = {t["name"]: t["skuId"] for t in src_tickets}
            for t in src_tickets:
                if t["name"] in cur_names:
                    continue
                addition = json.loads(t.get("skuAddition") or "{}")
                addition["bindId"] = str(uuid.uuid4())
                when = _not_started_window_ms(e["name"], [addition])
                if when:                        # 骨架没带开售窗的「未开售场次」→ 兜底
                    addition["fixedSaleTime"] = when
                    log(f"        骨架的「{e['name']}」票档「{t['name']}」没有开售时间窗"
                        f" → 兜底补一个未来时间")
                b._request("POST", "/v1/selfProduct/tickets/add",
                           json={"storeId": int(b.store_id), "productId": pid, "eventId": evid,
                                 "name": t["name"], "salePrice": t["salePrice"],
                                 "saleStatus": t.get("saleStatus", 1), "badge": t.get("badge", ""),
                                 "totalStockNum": t["totalStockNum"],
                                 "skuAddition": json.dumps(addition, ensure_ascii=False)})
            cur = b._request("GET", "/v1/selfProduct/tickets/list",
                             params={"storeId": b.store_id, "productId": pid, "eventId": evid})
            sku_map[e["name"]] = {t["name"]: {"skuId": t["skuId"],
                                              "price": str(t["salePrice"])} for t in cur}
        log(f"  [3/6] 票档已建：{ {k: len(v) for k, v in sku_map.items()} }")

    # 3b) 年龄限购（项目/场次/票档的克隆都不带它，必须单独搬，否则「18+可购场次」是空壳）
    _clone_age_limits(
        b, donor.product_id, pid,
        [{"donorEventId": e["productEventId"],
          "newEventId": name2new[e["name"]]["productEventId"],
          "eventName": e["name"],
          "age18": _is_age18(e["name"]),
          "donorSkus": donor_sku_map.get(e["name"], {}),
          "newSkus": {k: v["skuId"] for k, v in (sku_map.get(e["name"]) or {}).items()}}
         for e in drows], log=log)


    # 4) 退款/销售配置（把骨架的规则重绑到新场次）
    sale = b._request("GET", f"/v1/shop/product/{donor.product_id}/saleConfig",
                      params={"storeId": b.store_id})
    rules = []
    donor_name2id = {e["name"]: e["productEventId"] for e in drows}
    for rule in sale.get("refundRuleList", []):
        relations = []
        for rel in rule.get("relations", []):
            src_name = next((n for n, i in donor_name2id.items()
                             if str(i) == str(rel.get("bizId"))), None)
            if src_name and src_name in name2new:
                relations.append({"bizType": rel["bizType"],
                                  "bizId": name2new[src_name]["productEventId"]})
        details = [{k: d[k] for k in ("startTime", "endTime", "refundMode", "refundAmount")}
                   for d in rule.get("ruleDetails", [])]
        rules.append({"relations": relations, "ruleDetails": details})
    b._request("POST", f"/v1/shop/product/{pid}/saleConfig",
               params={"storeId": b.store_id},
               json={"storeId": int(b.store_id), "activityId": pid,
                     "refundSupport": sale["refundSupport"],
                     "inventoryRecoverySupport": sale["inventoryRecoverySupport"],
                     "refundTicketAllowTimes": sale["refundTicketAllowTimes"],
                     "refundRuleConfigStatus": sale["refundRuleConfigStatus"],
                     "purchaseStatus": sale["purchaseStatus"],
                     "purchaseTimeStatus": sale["purchaseTimeStatus"],
                     "tips": sale.get("tips", ""), "refundRuleList": rules})
    log("  [4/6] 退款/销售配置已写入")

    # 5) 上架（API 建项目不会自动上架 —— 漏了这步 C 端就是"商品已下架"）
    b._request("POST", f"/v1/shopTrade/product/{pid}/onSale/true",
               params={"storeId": b.store_id})
    log("  [5/6] 已上架")

    # 6) 回读复核
    chk = b._request("GET", "/v1/selfProduct/list",
                     params={"storeId": b.store_id, "pageIndex": 1, "pageSize": 50,
                             "activityName": bp.name})
    rows = chk.get("data") or chk.get("list") or []
    me = next((r for r in rows if str(r.get("id") or r.get("productId")) == str(pid)), None)
    on_sale = me.get("onSale") if me else None
    log(f"  [6/6] 回读复核：onSale={on_sale}")

    rec = {
        "productId": pid, "key": bp.key, "name": bp.name, "env": bp.env,
        "seat": bp.seat, "realName": bp.real_name, "delivery": bp.delivery,
        "donorKey": bp.donor_key,
        # 来源环境要求（供登记进清单时写备注 / 打用途标签）
        "requirement": bp.requirement.raw,
        "requirementUsages": list(bp.requirement.kwargs.get("usage") or []),
        # 场次的 maxVerifyCount 取**新项目回读值**（不是骨架值）—— 新建时事件只传了
        # eventName/startTime/endTime，核销次数由 B 端默认决定，登记必须照实回填，
        # 否则 drift --deep 会报"核销次数漂移"。
        "events": {e["name"]: {"eventId": name2new[e["name"]]["productEventId"],
                              "skus": sku_map.get(e["name"], {}),
                              "maxVerifyCount": name2new[e["name"]].get("maxVerifyCount")}
                   for e in drows},
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "onSale": on_sale,
    }
    led[led_key] = rec
    save_ledger(led)
    log(f"  账本已记账 → {LEDGER.name}")
    return rec


def _guard_write_env(env: str) -> None:
    if env == "prod" and os.environ.get("MIGU_ALLOW_PROD_WRITE") != "1":
        raise FactoryError("生产环境建项目需要 MIGU_ALLOW_PROD_WRITE=1（并在命令行加 --yes）")


def register_to_catalog(rec: dict, log=print) -> None:
    """把新建项目登记进商品清单。

    为什么不用 `catalog_ctl add`：那条命令一次只能挂**一个场次**，而 --update 会整体覆盖
    该项目条目 —— 多场次项目会被后一次调用清掉前面的场次。所以这里按同一份 schema
    直接合并进 catalog.json（写前备份），语义与 catalog_ctl 一致。
    """
    from migu_api.catalog import catalog_file
    path = catalog_file()
    backup = path.with_suffix(path.suffix + ".bak.casegen")
    raw = json.loads(path.read_text(encoding="utf-8"))
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    env_block = raw.setdefault("environments", {}).setdefault(rec["env"], {})
    projects = env_block.setdefault("projects", [])
    req_usage = list(rec.get("requirementUsages") or []) \
        if rec.get("requirementUsages") is not None else (
            list(rec["requirement_obj"].kwargs.get("usage") or [])
            if rec.get("requirement_obj") else [])

    entry = {
        "key": rec["key"],
        "name": rec["name"],
        "productId": rec["productId"],
        "status": "ready" if rec.get("onSale") is not False else "rework",
        "note": f"由 tools/casegen 自动创建（{rec['createdAt']}，骨架 {rec['donorKey']}）"
                f"｜来源用例环境要求：{rec.get('requirement') or '—'}"
                + ("" if rec.get("onSale") is not False else "｜⚠️ 尚未确认上架，请核对 onSale"),
        "profile": {
            "seat": rec["seat"],
            "realName": rec["realName"],
            "delivery": rec["delivery"],
            "deliveryType": _DELIVERY_TYPES.get(rec["delivery"], []),
        },
        "usages": req_usage,
        "events": [],
    }
    for ev_name, ev in rec["events"].items():
        skus = []
        for sku_name, info in (ev.get("skus") or {}).items():
            # ★ 不自动打 free_fixture：该标签在清单里必须**唯一**
            #   （catalog.one(usage="free_fixture") 要求恰好命中 1 条，多处命中会直接抛
            #   CatalogLookupError 打断 test_b_verify / test_b_refund / test_catalog）。
            #   用途标签只按环境要求显式带过来，其余留空 —— 与 P02–P11 的登记口径一致。
            skus.append({"key": _sku_key(sku_name), "name": sku_name,
                         "skuId": info["skuId"],
                         "price": str(info.get("price", "0")), "usages": []})
        ev_verify = ev.get("maxVerifyCount")
        entry["events"].append({"key": _slug(ev_name), "eventId": ev["eventId"],
                                "name": ev_name,
                                "maxVerifyCount": (ev_verify if ev_verify is not None
                                                   else rec.get("maxVerifyCount", 1)),
                                "skus": skus})

    idx = next((i for i, p in enumerate(projects) if str(p.get("key")) == rec["key"]), None)
    if idx is None:
        projects.append(entry)
    else:
        projects[idx] = entry
    raw["updated"] = time.strftime("%Y-%m-%d")
    v = raw.get("version")
    if isinstance(v, (int, float)):
        raw["version"] = round(float(v) + 0.01, 2)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  已登记进清单 {path.name}（备份 {backup.name}）："
        f"{rec['key']} productId={rec['productId']} "
        f"{len(entry['events'])} 场次 / "
        f"{sum(len(e['skus']) for e in entry['events'])} 票档")


def _slug(name: str) -> str:
    """场次名 → 清单用的 key（与 P01–P11 的登记口径对齐）。"""
    table = {"条件退场次": "refundable", "非条件退场次": "non_refundable",
             "多次核销场次": "multi_verify", "年龄限购场次": "age18",
             "18+可购场次": "age18", "未开售场次": "not_started",
             "常规场次": "regular", "第二常规场次": "regular2",
             "已结束场次": "expired"}
    return table.get(name, name or "regular")


def _sku_key(name: str) -> str:
    """票档名 → 清单用的 key（与 P01–P11 的登记口径对齐）。"""
    table = {"0元票档": "free", "0.01元票档": "paid", "未开售0元": "not_started",
             "限购2张": "limit2", "年龄限购票档": "age18",
             "VVIP票档": "vvip", "VIP票档": "vip", "C票档": "c", "B票档": "b",
             "A票档": "a", "免付票档": "free", "有价票档": "paid",
             "选座免付票档": "free", "选座有价票档": "paid"}
    return table.get(name, name or "sku")


def _ms2str(ms: int) -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(
        ms / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
