# -*- coding: utf-8 -*-
"""
resolve.py —— 把用例的「环境要求」翻译成商品清单里的**具体可下单目标**

要解决的问题
    用例里写「前置条件：已生成一条待领取邀请函（0 元电子票、非选座项目）」时，
    执行者还需要自己去找：哪个项目？哪个场次？哪个票档？这个文件负责把这句话
    变成 `[P01] 非选座-非实名-电子票-yb / 条件退场次 / 0元票档 → productId=222945 skuId=10637339`，
    找不到就明确说**缺什么样的项目**（交给 factory 去建）。

环境要求写法（规格文件「环境要求」列，多条用逗号分隔）
    seat=false            选座 / 非选座
    realName=0            0 非实名 / 1 一票一证 / 2 一单一证
    delivery=eticket      eticket 电子票 / paper 纸质票 / idcard 证件入场
    usage=free_fixture    清单用途标签：baseline / free_fixture / paid / partial_refund /
                          multi_verify / refund / age18 …
    price=0               票价精确匹配（0 = 0 元免付）
    maxVerify>=2          核销次数下限（>=2 即"多次核销"）
    maxVerify=1           核销次数精确匹配
    event=multi_verify    场次 key（refundable / non_refundable / multi_verify / age18 / not_started …）
    project=P01           直接锁定项目
    sku=free              直接锁定票档 key
    any=[seat,realName]   可选：这些维度"有就行"，不参与筛选（多形态用例用）

用法
    from tools.casegen.resolve import resolve_requirements, Requirement
    reqs = collect_requirements(suite)          # 规格里出现的去重后环境要求
    for req in reqs:
        r = resolve_requirements(req)           # -> Resolution
        r.ok / r.target.label / r.missing_hint
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from migu_api import catalog
from migu_api.catalog import CatalogError, Target

# 规格里常见的中文写法 → 清单字段
_ALIASES = {
    "seat": "seat", "选座": "seat",
    "realname": "real_name", "real_name": "real_name", "实名": "real_name",
    "delivery": "delivery", "交付": "delivery", "交付方式": "delivery",
    "usage": "usage", "usages": "usage", "用途": "usage",
    "price": "price", "价格": "price", "票价": "price",
    "maxverify": "max_verify", "max_verify": "max_verify", "核销次数": "max_verify",
    "event": "event", "场次": "event",
    "project": "project", "项目": "project",
    "sku": "sku", "票档": "sku",
}


# 这些场次 key 有额外语义（未开售 / 已结束），不该被当作「默认目标」——
# 只有用例显式写 event=<key> 时才应命中。
_SPECIAL_EVENTS = {"not_started", "expired"}


@dataclass
class Requirement:
    """一条环境要求（已解析）。"""

    raw: str
    kwargs: dict = field(default_factory=dict)      # 传给 catalog.find 的条件
    exact_max_verify: int | None = None             # maxVerify=N 的精确匹配
    note: str = ""                                  # 解析告警（如未知维度）

    def __str__(self) -> str:
        return self.raw


@dataclass
class Resolution:
    """一条环境要求的解析结果。"""

    requirement: Requirement
    targets: list[Target] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.targets)

    @property
    def best(self) -> Target | None:
        return self.targets[0] if self.targets else None

    @property
    def label(self) -> str:
        t = self.best
        return t.label if t else "（无匹配项目）"

    def missing_hint(self) -> str:
        """找不到时，给出「需要建什么样的项目」的人话描述。"""
        kw = self.requirement.kwargs
        bits = []
        if "seat" in kw:
            bits.append("选座" if kw["seat"] else "非选座")
        if "real_name" in kw:
            bits.append({0: "非实名", 1: "一票一证", 2: "一单一证"}.get(kw["real_name"],
                                                                    f"实名模式{kw['real_name']}"))
        if "delivery" in kw:
            bits.append({"eticket": "电子票", "paper": "纸质票", "idcard": "证件入场"}.get(
                kw["delivery"], kw["delivery"]))
        if kw.get("usage"):
            bits.append("用途=" + "/".join(sorted(kw["usage"])))
        if self.requirement.exact_max_verify:
            bits.append(f"核销次数={self.requirement.exact_max_verify}")
        if kw.get("price") is not None:
            bits.append(f"票价={kw['price']}元")
        if kw.get("event"):
            bits.append(f"场次={kw['event']}")
        return "、".join(bits) if bits else self.requirement.raw


# ------------------------------------------------------------------ 解析

def _coerce(name: str, value: str):
    v = value.strip()
    if name == "seat":
        return v.lower() in ("1", "true", "yes", "y", "是", "选座")
    if name in ("real_name", "max_verify", "price"):
        try:
            return int(float(v))
        except ValueError:
            return v
    return v


def parse_requirement(raw: str) -> Requirement:
    """把一行「环境要求」文本解析成 Requirement。"""
    req = Requirement(raw=raw.strip())
    parts = [p for p in re.split(r"[,，;；]", raw) if p.strip()]
    for part in parts:
        m = re.match(r"\s*([A-Za-z_\u4e00-\u9fa5]+)\s*(>=|<=|!=|=|>|<)\s*(.+?)\s*$", part)
        if not m:
            req.note = f"无法解析的条件「{part.strip()}」（写法应形如 seat=false）"
            continue
        key, op, val = m.group(1).lower(), m.group(2), m.group(3)
        name = _ALIASES.get(key)
        if name is None:
            req.note = f"未知维度「{key}」（可用：seat/realName/delivery/usage/price/maxVerify/event/project/sku）"
            continue
        if name == "max_verify":
            n = _coerce("max_verify", val)
            if op == "=":
                req.exact_max_verify = int(n)
            elif op == ">=":
                req.kwargs["min_verify"] = int(n)
            else:
                req.note = f"maxVerify 只支持 = 与 >=（收到 {op}）"
            continue
        if name == "usage":
            req.kwargs.setdefault("usage", []).extend(
                [x for x in re.split(r"[+/|]", val) if x.strip()])
            continue
        if op != "=":
            req.note = f"「{key}」只支持等号匹配（收到 {op}）"
            continue
        req.kwargs[name] = _coerce(name, val)
    return req


# ------------------------------------------------------------------ 匹配

def resolve_requirements(raw: str, env: str | None = None) -> Resolution:
    """在指定环境的清单里找目标。"""
    req = parse_requirement(raw)
    try:
        cat = catalog.load(env) if env else catalog.load()
    except CatalogError as e:
        return Resolution(req, error=str(e))
    hits = cat.find(**req.kwargs)
    if req.exact_max_verify is not None:
        hits = [t for t in hits if (t.max_verify_count or 1) == req.exact_max_verify]
    # 排序：先免费、再 ready、再避开「特殊场次」、再便宜 → 结果稳定可复现
    #   ★ 用例没写 event= 时，不该被「未开售 / 已结束」场次抢到默认目标：
    #     实测 P12 的 0 元档落在「未开售场次」，会让执行者拿着"未开售"的场次去做领取用例。
    #     显式写了 event=not_started 时照旧命中（这里的排序只在未指定 event 时起作用）。
    hits.sort(key=lambda t: (not t.is_free, t.status != "ready",
                             t.event_key in _SPECIAL_EVENTS,
                             float(t.price or 0),
                             t.project_key, t.sku_id))
    return Resolution(req, targets=hits)


def collect_requirements(suite) -> list[str]:
    """按出现顺序收集规格里所有不重复的「环境要求」，并保留哪些用例用到它。"""
    seen: dict[str, list[str]] = {}
    for case in suite.cases:
        r = (case.requires or "").strip()
        if r:
            seen.setdefault(r, []).append(case.case_id)
    return list(seen.keys())


def requirement_case_map(suite) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for case in suite.cases:
        r = (case.requires or "").strip()
        if r:
            out.setdefault(r, []).append(case.case_id)
    return out


def plan(suite, env: str | None = None) -> list[tuple[str, Resolution, list[str]]]:
    """对规格里每条环境要求做一次匹配，返回 (要求, 结果, 用到的用例编号)。"""
    cmap = requirement_case_map(suite)
    return [(raw, resolve_requirements(raw, env), cmap.get(raw, []))
            for raw in collect_requirements(suite)]
