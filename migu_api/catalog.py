# -*- coding: utf-8 -*-
"""
catalog.py —— 「测试专用商品清单」：C 端下单的**唯一白名单** + 按需查找入口

为什么要有这个东西
    C 端的下单用例（核心链路 / 真实支付 / 核销夹具 / 部分退）以前各自在代码里
    写死商品 ID，或从 config 的 `paid_test_product` / `fixture_free_product` 取。
    这带来三个问题：
      1. 商品散落在各处（用例、config、手写脚本），改了 B 端配置没人提醒你；
      2. 谁都能顺手拿一个"看着顺眼"的商品下单 → 测试数据不可控、库存被误耗；
      3. 想按「选座 / 纸质票 / 实名档位 / 多次核销」挑一个合适的目标，只能翻文档。

    所以把「允许被下单的测试项目」收敛成一份**动态清单**（项目根 `catalog.json`），
    用例按「项目类型 → 场次 → 票档」去查找，而不是写死 ID；
    `MiguClient.create_order()` 在下单前强制比对清单，落在清单外直接拦掉。

清单长什么样（catalog.json）
    environments.<test|prod>.projects[]
        项目级：key / name / productId / status / profile{seat, realName, delivery}
               usages[]（baseline / free_fixture / paid / partial_refund / multi_verify ...）
        场次级：events[]  key / eventId / name / maxVerifyCount
        票档级：skus[]    key / skuId / price / usages[]

    只有 `productId` 与 `skuId` 都登记了的条目才算「可下单」；
    `status = planned`（待建）或没登记 SKU 的项目，自然落不进白名单。

怎么用（用例侧）
    from migu_api import catalog

    # 1) 按用途取一个目标（最常见）
    t = catalog.one(usage="free_fixture")          # 0 元免付夹具
    t = catalog.one(usage="paid")
    # 2) 按维度查找
    t = catalog.one(seat=True, real_name=1, delivery="eticket")
    t = catalog.one(project="P01", event="multi_verify")
    # 3) 拿到就能直接下单
    order_id = client.create_order(t.product_id, t.sku_id, 1, t.price)

    # client.create_order 内部会自动调 catalog.guard_order()：
    # 不在清单里的 (productId, skuId) 抛 CatalogViolation，防止"顺手乱下单"。

怎么新增项目（无需改代码）
    ./run.sh catalog list                    # 看当前清单
    ./run.sh catalog find --usage paid       # 按用途查找
    ./run.sh catalog add --env test --key P08 --product-id 222xxx --sku ...   # 追加
    或直接编辑 catalog.json（格式见上）。

开关
    MIGU_CATALOG_ENFORCE=0   临时关掉白名单拦截（只做排查用，正常回归不要关）。
    MIGU_CATALOG_FILE=<路径>  指定别的清单文件（CI 里按分支切清单时用）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 清单文件：默认在项目根（allure-demo/catalog.json），可用环境变量指到别处
DEFAULT_CATALOG_FILE = Path(__file__).resolve().parent.parent / "catalog.json"
CATALOG_FILE_ENV = "MIGU_CATALOG_FILE"
# 白名单拦截开关（默认开）。取值见 _truthy()
ENFORCE_ENV = "MIGU_CATALOG_ENFORCE"

_ON = {"1", "true", "yes", "y", "on"}
_OFF = {"0", "false", "no", "n", "off"}


class CatalogError(AssertionError):
    """清单本身有问题（文件不存在 / JSON 非法 / 环境段缺失）。

    继承 AssertionError：pytest 会把它当"用例失败"呈现，而不是收集期错误，
    这样其它不依赖清单的用例还能正常跑。
    """


class CatalogViolation(CatalogError):
    """下单目标不在清单白名单内 —— 这是**有意的拦截**，不是 bug。"""


class CatalogLookupError(CatalogError):
    """按条件找不到目标（或找到多条分不清）。"""


# ---------------------------------------------------------------- 目标

@dataclass(frozen=True)
class Target:
    """一个「可下单的票档」= 项目 × 场次 × 票档 的完整定位。"""

    env: str
    project_key: str
    project_name: str
    product_id: int
    status: str
    seat: bool | None
    real_name: int | None
    delivery: str | None
    delivery_type: tuple
    project_usages: frozenset
    event_key: str
    event_name: str
    event_id: int | None
    max_verify_count: int | None
    sku_key: str
    sku_name: str
    sku_id: int
    price: str
    sku_usages: frozenset
    # 清单里项目 / 场次 / 票档上的 note，便于把"为什么用它"写进报告
    project_note: str = ""
    event_note: str = ""
    sku_note: str = ""

    @property
    def usages(self) -> frozenset:
        """项目级 + 票档级的用途标签并集（查找时按这个匹配）。"""
        return self.project_usages | self.sku_usages

    @property
    def amount(self) -> str:
        """下单时要传的 totalAmount（字符串，与前端 toFixed(2) 对齐）。"""
        return self.price

    @property
    def is_free(self) -> bool:
        try:
            return float(self.price) == 0.0
        except (TypeError, ValueError):
            return False

    def has(self, *usages: str) -> bool:
        return all(u in self.usages for u in usages)

    @property
    def label(self) -> str:
        """一行摘要，写进 Allure / 日志里一眼看懂。"""
        return (
            f"[{self.project_key}] {self.project_name} / {self.event_name}"
            f" / {self.sku_name}({self.price}元) → productId={self.product_id} "
            f"skuId={self.sku_id}"
        )

    def as_dict(self) -> dict:
        return {
            "projectKey": self.project_key, "projectName": self.project_name,
            "productId": self.product_id, "status": self.status,
            "seat": self.seat, "realName": self.real_name, "delivery": self.delivery,
            "deliveryType": list(self.delivery_type),
            "usages": sorted(self.usages),
            "eventKey": self.event_key, "eventId": self.event_id,
            "maxVerifyCount": self.max_verify_count,
            "skuKey": self.sku_key, "skuId": self.sku_id, "price": self.price,
        }


# ---------------------------------------------------------------- 清单

@dataclass(frozen=True)
class Project:
    """清单里的一个项目（可能还没建好 → status=planned）。"""

    key: str
    name: str
    product_id: int | None
    status: str
    note: str
    seat: bool | None
    real_name: int | None
    delivery: str | None
    delivery_type: tuple
    usages: frozenset
    events: tuple

    @property
    def orderable(self) -> bool:
        """登记了 productId 且至少有一个登记了 skuId 的票档 → 可下单。"""
        if self.product_id is None:
            return False
        return any(
            s.get("skuId") is not None
            for e in self.events for s in (e.get("skus") or [])
        )

    @property
    def status_text(self) -> str:
        return {
            "ready": "已建好可用",
            "rework": "可复用·待改配置",
            "planned": "待新建",
        }.get(self.status, self.status)


class Catalog:
    """某个环境（test / prod）的测试专用商品清单。"""

    def __init__(self, raw: dict, env_name: str, source: Path | None = None):
        self.raw = raw
        self.env_name = env_name
        self.source = source
        self.version = raw.get("version")
        self.updated = raw.get("updated")
        envs = raw.get("environments") or {}
        env = envs.get(env_name)
        if env is None:
            raise CatalogError(
                f"清单 {source} 里没有环境段 environments.{env_name} "
                f"（现有：{', '.join(envs) or '无'}）"
            )
        self.store_id = str(env.get("storeId") or "")
        self.site = env.get("site") or ""
        self.env_note = env.get("note") or ""
        self.legend: dict = raw.get("legend") or {}
        self.projects: tuple[Project, ...] = tuple(
            self._parse_project(p) for p in (env.get("projects") or [])
        )
        self._targets: list[Target] | None = None

    # ---- 解析 ----

    @staticmethod
    def _parse_project(p: dict) -> Project:
        prof = p.get("profile") or {}
        return Project(
            key=str(p.get("key") or "?"),
            name=p.get("name") or "",
            product_id=p.get("productId"),
            status=str(p.get("status") or "ready"),
            note=p.get("note") or "",
            seat=prof.get("seat"),
            real_name=prof.get("realName"),
            delivery=prof.get("delivery"),
            delivery_type=tuple(prof.get("deliveryType") or ()),
            usages=frozenset(p.get("usages") or ()),
            events=tuple(p.get("events") or ()),
        )

    # ---- 目标展开 ----

    def targets(self, include_planned: bool = False) -> list[Target]:
        """展开成「可下单票档」列表。

        include_planned=False（默认）只返回 productId 与 skuId 都登记了的条目 ——
        也就是白名单真实放行的集合。
        """
        if self._targets is None:
            self._targets = self._expand()
        if include_planned:
            return list(self._targets)
        return [t for t in self._targets if t.product_id is not None]

    def _expand(self) -> list[Target]:
        out: list[Target] = []
        for p in self.projects:
            events = p.events or ({},)
            for e in events:
                skus = (e or {}).get("skus") or [{}]
                for s in skus:
                    sku_id = (s or {}).get("skuId")
                    if p.product_id is None or sku_id is None:
                        # 未建成的项目 / 未登记的票档：保留一条"占位"，
                        # 供 find / describe 提示"这项还没建"，但不会被下单。
                        continue
                    out.append(Target(
                        env=self.env_name,
                        project_key=p.key,
                        project_name=p.name,
                        product_id=int(p.product_id),
                        status=p.status,
                        seat=p.seat,
                        real_name=p.real_name,
                        delivery=p.delivery,
                        delivery_type=p.delivery_type,
                        project_usages=p.usages,
                        event_key=str((e or {}).get("key") or "default"),
                        event_name=(e or {}).get("name") or "",
                        event_id=(e or {}).get("eventId"),
                        max_verify_count=(e or {}).get("maxVerifyCount"),
                        sku_key=str((s or {}).get("key") or "default"),
                        sku_name=(s or {}).get("name") or "",
                        sku_id=int(sku_id),
                        price=str((s or {}).get("price") or ""),
                        sku_usages=frozenset((s or {}).get("usages") or ()),
                        project_note=p.note,
                        event_note=(e or {}).get("note") or "",
                        sku_note=(s or {}).get("note") or "",
                    ))
        return out

    def project(self, key_or_pid: str | int) -> Project:
        for p in self.projects:
            if str(p.key) == str(key_or_pid) or (
                p.product_id is not None and str(p.product_id) == str(key_or_pid)
            ):
                return p
        raise CatalogLookupError(
            f"清单里没有项目 {key_or_pid!r}。现有项目："
            + ", ".join(f"{p.key}({p.status})" for p in self.projects)
        )

    # ---- 查找 ----

    def find(self, **criteria) -> list[Target]:
        """按条件查找可下单目标（AND 语义）。

        条件（都可省略）：
            project     项目 key（P01）或 productId
            event       场次 key（regular / multi_verify）或 eventId
            sku         票档 key 或 skuId
            usage       用途标签，可传一个或多个（要求全部命中）
            seat        True/False —— 选座 / 非选座
            real_name   0 非实名 / 1 一票一证 / 2 一单一证
            delivery    eticket / paper / idcard
            price       精确价格（"0.00" 或 0.0 都行）
            max_price   价格上限（含）
            min_verify  核销次数下限（含）—— min_verify=2 即"多次核销"
            status      ready / rework
        """
        want = _normalize(criteria)
        return [t for t in self.targets() if _match(t, want)]

    def one(self, **criteria) -> Target:
        """找**唯一**目标；0 条或多条都抛错并列出候选，避免"默默用了另一个商品"。"""
        hits = self.find(**criteria)
        if len(hits) == 1:
            return hits[0]
        desc = _criteria_text(criteria)
        if not hits:
            raise CatalogLookupError(
                f"清单里找不到满足 {desc} 的可下单商品。\n"
                f"当前环境 [{self.env_name}] 清单里的可下单目标：\n"
                + "\n".join(f"  · {t.label}" for t in self.targets())
                + "\n(若这是新项目，请先在 B 端建好再执行 ./run.sh catalog sync 或 "
                  "./run.sh catalog add 登记)"
            )
        raise CatalogLookupError(
            f"{desc} 命中 {len(hits)} 条，无法确定用哪个，请再收紧条件：\n"
            + "\n".join(f"  · {t.label}" for t in hits[:12])
        )

    def allowed(self, product_id: int | str, sku_id: int | str | None = None) -> Target | None:
        """(productId, skuId) 是否在清单白名单内；命中则返回对应 Target。"""
        for t in self.targets():
            if str(t.product_id) != str(product_id):
                continue
            if sku_id is None or str(t.sku_id) == str(sku_id):
                return t
        return None

    # ---- 展示 ----

    def describe(self, only_status: str | None = None) -> str:
        """人类可读的清单文本（run.sh catalog list 用）。"""
        lines = [
            f"测试专用商品清单  ·  环境 [{self.env_name}]  storeId={self.store_id}"
            f"  版本 v{self.version}  更新 {self.updated}",
            f"来源 {self.source}",
            "=" * 78,
        ]
        if self.env_note:
            lines.append(self.env_note)
            lines.append("-" * 78)
        for p in self.projects:
            if only_status and p.status != only_status:
                continue
            flag = {"ready": "✔", "rework": "△", "planned": "✗"}.get(p.status, "?")
            pid = p.product_id if p.product_id is not None else "—"
            prof = (f"选座={'是' if p.seat else '否'} "
                    f"实名={p.real_name} 交付={p.delivery or '—'}")
            lines.append(f"{flag} {p.key:<4} {p.name:<26} productId={str(pid):<8} "
                         f"{p.status_text:<10} {prof}")
            for t in self.targets():
                if t.project_key != p.key:
                    continue
                verify = (f" 核销×{t.max_verify_count}"
                          if t.max_verify_count and t.max_verify_count > 1 else "")
                lines.append(f"      └ {t.event_key:<16} {t.event_name[:28]:<30} "
                             f"skuId={t.sku_id:<10} {t.price}元{verify}"
                             f"  [{', '.join(sorted(t.usages))}]")
        total = len(self.targets())
        ready = sum(1 for p in self.projects if p.status == "ready")
        lines.append("-" * 78)
        lines.append(f"可下单目标 {total} 个 ｜ 项目 {len(self.projects)} 个"
                     f"（ready {ready} / rework "
                     f"{sum(1 for p in self.projects if p.status == 'rework')} / planned "
                     f"{sum(1 for p in self.projects if p.status == 'planned')}）")
        return "\n".join(lines)


# ---------------------------------------------------------------- 匹配与工具

def _normalize(criteria: dict) -> dict:
    """把 find/one 的关键字参数规范化。"""
    want: dict[str, Any] = {}
    for k, v in criteria.items():
        if v is None:
            continue
        if k == "usages":          # 允许 find(usages=[...])
            want.setdefault("usage", []).extend(_as_list(v))
        elif k == "usage":
            want.setdefault("usage", []).extend(_as_list(v))
        else:
            want[k] = v
    return want


def _as_list(v) -> list:
    if isinstance(v, str):
        return [v]
    if isinstance(v, Iterable):
        return list(v)
    return [v]


def _match(t: Target, want: dict) -> bool:
    for k, v in want.items():
        if k == "project":
            if str(t.project_key) != str(v) and str(t.product_id) != str(v):
                return False
        elif k == "event":
            if str(t.event_key) != str(v) and str(t.event_id) != str(v):
                return False
        elif k == "sku":
            if str(t.sku_key) != str(v) and str(t.sku_id) != str(v):
                return False
        elif k == "usage":
            if not all(u in t.usages for u in v):
                return False
        elif k == "seat":
            if bool(t.seat) is not bool(v):
                return False
        elif k == "real_name":
            if t.real_name != v:
                return False
        elif k == "delivery":
            if t.delivery != v:
                return False
        elif k == "status":
            if t.status != v:
                return False
        elif k == "price":
            if float(t.price or 0) != float(v):
                return False
        elif k == "max_price":
            if float(t.price or 0) > float(v):
                return False
        elif k == "min_verify":
            if (t.max_verify_count or 0) < int(v):
                return False
        else:
            raise CatalogError(f"find/one 不支持的条件：{k!r}")
    return True


def _criteria_text(criteria: dict) -> str:
    if not criteria:
        return "（无条件）"
    return " / ".join(f"{k}={v}" for k, v in criteria.items()
                      if v is not None)


# ---------------------------------------------------------------- 加载与单例

_cache: dict[str, Catalog] = {}


def catalog_file() -> Path:
    return Path(os.environ.get(CATALOG_FILE_ENV) or DEFAULT_CATALOG_FILE)


def load(env_name: str | None = None) -> Catalog:
    """加载某个环境的清单（带缓存）。env_name 默认取 MIGU_ENV，与 config 同规则。"""
    name = (env_name or os.environ.get("MIGU_ENV", "test")).lower()
    if name in _cache:
        return _cache[name]
    path = catalog_file()
    if not path.exists():
        raise CatalogError(
            f"找不到测试专用商品清单：{path}\n"
            f"这是 C 端下单的白名单来源，必须有。"
            f"（可用 {CATALOG_FILE_ENV} 指向别的文件）"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"清单 {path} JSON 解析失败：{exc}") from exc
    cat = Catalog(raw, name, source=path)
    _cache[name] = cat
    return cat


def reload() -> None:
    """清缓存（run.sh catalog 子命令改完文件后刷新用）。"""
    _cache.clear()


def current() -> Catalog:
    """当前环境的清单单例。"""
    return load()


def reset(env_name: str | None = None) -> None:
    """丢掉缓存并重新加载（测试里改文件后调用）。"""
    if env_name:
        _cache.pop(env_name.lower(), None)
    else:
        _cache.clear()


# ---------------------------------------------------------------- 模块级便捷入口
# 用例里最常用的两个（等价于 current().find(...) / current().one(...)）：
#     t = catalog.one(usage="free_fixture")
#     ts = catalog.find(seat=True, real_name=1)

def find(**criteria) -> list[Target]:
    """在当前环境的清单里查找可下单目标（AND 语义，条件见 Catalog.find）。"""
    return current().find(**criteria)


def one(**criteria) -> Target:
    """查找**唯一**目标；0 条或多条都会抛 CatalogLookupError 并列出候选。"""
    return current().one(**criteria)


# ---------------------------------------------------------------- 白名单闸门

def enforce_enabled() -> bool:
    """白名单拦截是否生效。默认开；MIGU_CATALOG_ENFORCE=0 关闭。"""
    raw = os.environ.get(ENFORCE_ENV)
    if raw is None or raw == "":
        return True
    token = str(raw).strip().lower()
    if token in _OFF:
        return False
    if token in _ON:
        return True
    raise CatalogError(f"{ENFORCE_ENV} 取值非法：{raw!r}（可用 1/0、on/off）")


def guard_order(product_id: int, sku_id: int, source: str = "") -> Target | None:
    """下单前的白名单校验 —— 不在清单内直接拦掉。

    client.create_order() 会自动调用，用例不需要手动调。

    返回命中的 Target（方便调用方把"这一单属于哪个测试项目"记进报告）；
    被关掉（MIGU_CATALOG_ENFORCE=0）时返回 None。
    """
    if not enforce_enabled():
        return None
    cat = current()
    hit = cat.allowed(product_id, sku_id)
    if hit is not None:
        return hit

    # 没命中：把"为什么被拦"讲清楚 —— 是项目压根没登记，还是只是这个 SKU 没登记
    same_project = [t for t in cat.targets()
                    if str(t.product_id) == str(product_id)]
    lines = [
        f"下单被拦截：productId={product_id} / skuId={sku_id} 不在"
        f"「测试专用商品清单」内（环境 {cat.env_name}）。",
        "C 端下单只允许落在清单登记的项目·场次·票档上，避免误耗其它商品的库存与产生不可解释的测试数据。",
    ]
    if source:
        lines.insert(0, f"来源：{source}")
    if same_project:
        lines.append(
            f"该项目已在清单里，但登记的票档是："
            + "、".join(f"skuId={t.sku_id}({t.sku_name})" for t in same_project)
        )
    else:
        known = {str(t.product_id) for t in cat.targets()}
        lines.append(f"清单里根本没有 productId={product_id}（现有："
                     + "、".join(sorted(known)) + "）")
    try:
        p = cat.project(product_id)
        if p.status == "planned":
            lines.append(f"⚠️ 项目 {p.key} 当前状态是「待新建」，还没登记 productId。")
        elif p.status == "rework":
            lines.append(f"ℹ️ 项目 {p.key} 状态是「可复用·待改配置」：{p.note}")
    except CatalogLookupError:
        pass
    lines += [
        f"清单文件：{cat.source}",
        "怎么加：编辑 catalog.json 对应环境的 projects[]，或执行 ./run.sh catalog add …",
        f"（确需绕过：{ENFORCE_ENV}=0，仅限排查，正常回归不要关）",
    ]
    raise CatalogViolation("\n".join(lines))


# ---------------------------------------------------------------- 给 config 用

def pair(env_name: str, usage: str) -> tuple[int, int] | None:
    """取 (productId, skuId)。给 config.fixture_free_product 这类字段派生用。"""
    try:
        t = load(env_name).one(usage=usage)
    except CatalogLookupError:
        return None
    return (t.product_id, t.sku_id)


def triple(env_name: str, usage: str) -> tuple[int, int, str] | None:
    """取 (productId, skuId, 价格字符串)。给 config.paid_test_product 用。"""
    try:
        t = load(env_name).one(usage=usage)
    except CatalogLookupError:
        return None
    return (t.product_id, t.sku_id, t.price)
