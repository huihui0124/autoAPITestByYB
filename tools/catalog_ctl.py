# -*- coding: utf-8 -*-
"""
catalog_ctl.py —— 「测试专用商品清单」管理命令（run.sh catalog 的背后实现）

    ./run.sh catalog list                      列出当前环境清单
    ./run.sh catalog list --env prod
    ./run.sh catalog find --usage paid         按用途查找
    ./run.sh catalog find --seat --real-name 1
    ./run.sh catalog show P01                  看某个项目的完整登记
    ./run.sh catalog check                     离线自检（结构 + 与 config 一致性）
    ./run.sh catalog check --online            再联网核对清单里的 SKU 是否还能下单
    ./run.sh catalog drift                     漂移巡检：清单 vs 环境实况（只读，CI 每日跑）
    ./run.sh catalog drift --env all           两端都查（各自起子进程）
    ./run.sh catalog drift --all --deep        再查 B 端场次/选座/实名配置（需 B 端会话）
    ./run.sh catalog add ...                   追加/更新一个项目（写回 catalog.json）

add 示例（新建项目 P08，选座 + 身份证件 + 一单一证，挂两个票档）：
    ./run.sh catalog add --env test --key P08 \
        --name "选座-身份证件-一单一证yb" --product-id 222999 \
        --seat --real-name 2 --delivery idcard \
        --usage seat --usage idcard \
        --event-key regular --event-id 7905001 \
        --sku 10650001:0.01 --sku 10650002:1.00

    说明：--sku 可重复，格式 skuId:价格[:名称]；都挂在 --event-key 指定的场次下。
          已存在同 key 项目时默认报错，加 --update 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migu_api import catalog  # noqa: E402


# ---------------------------------------------------------------- list / show / find

def cmd_list(args) -> int:
    cat = catalog.load(args.env)
    print(cat.describe(only_status=args.status))
    return 0


def cmd_show(args) -> int:
    cat = catalog.load(args.env)
    p = cat.project(args.project)
    print(f"[{p.key}] {p.name}")
    print(f"  状态      : {p.status_text}（{p.status}）")
    print(f"  productId : {p.product_id if p.product_id is not None else '—（未建）'}")
    print(f"  项目级    : 选座={'是' if p.seat else '否'} / 实名={p.real_name} "
          f"/ 交付={p.delivery or '—'} {list(p.delivery_type) or ''}")
    print(f"  用途标签  : {', '.join(sorted(p.usages)) or '—'}")
    if p.note:
        print(f"  备注      : {p.note}")
    print("  场次 / 票档：")
    hits = [t for t in cat.targets() if t.project_key == p.key]
    if not hits:
        print("    （无已登记票档 → 不可下单）")
    for t in hits:
        print(f"    · {t.event_key:<16} {t.event_name}")
        print(f"        eventId={t.event_id} 核销次数={t.max_verify_count}"
              f"  →  skuId={t.sku_id} {t.price}元 [{', '.join(sorted(t.usages))}]")
    return 0


def cmd_find(args) -> int:
    cat = catalog.load(args.env)
    kw = {}
    if args.project:
        kw["project"] = args.project
    if args.event:
        kw["event"] = args.event
    if args.sku:
        kw["sku"] = args.sku
    if args.usage:
        kw["usage"] = args.usage
    if args.seat is not None:
        kw["seat"] = args.seat
    if args.real_name is not None:
        kw["real_name"] = args.real_name
    if args.delivery:
        kw["delivery"] = args.delivery
    if args.price:
        kw["price"] = args.price
    if args.max_price:
        kw["max_price"] = args.max_price
    if args.min_verify:
        kw["min_verify"] = args.min_verify
    hits = cat.find(**kw)
    if not hits:
        print(f"没有满足 {kw} 的可下单目标。")
        return 1
    print(f"命中 {len(hits)} 条：")
    for t in hits:
        print(f"  · {t.label}")
        print(f"      → client.create_order({t.product_id}, {t.sku_id}, 1, \"{t.price}\")")
    return 0


# ---------------------------------------------------------------- check

def cmd_check(args) -> int:
    ok = True
    for env_name in ("test", "prod"):
        try:
            cat = catalog.load(env_name)
        except catalog.CatalogError as exc:
            print(f"[{env_name}] ✗ 加载失败：{exc}")
            ok = False
            continue
        n_targets = len(cat.targets())
        n_projects = len(cat.projects)
        print(f"[{env_name}] 项目 {n_projects} 个 / 可下单目标 {n_targets} 个 "
              f"/ storeId={cat.store_id}")

        # 结构自检
        for p in cat.projects:
            if p.status == "ready" and p.product_id is None:
                print(f"  ✗ {p.key} status=ready 但没有 productId")
                ok = False
            if p.status == "planned":
                leak = [t for t in cat.targets() if t.project_key == p.key]
                if leak:
                    print(f"  ✗ {p.key} status=planned 却进了白名单")
                    ok = False
            for t in cat.targets():
                if t.project_key == p.key:
                    try:
                        float(t.price)
                    except (TypeError, ValueError):
                        print(f"  ✗ {p.key}/{t.sku_id} 价格非法 {t.price!r}")
                        ok = False

        # 与 config 派生值一致性
        if env_name == "test":
            from migu_api.config import TEST as prof
        else:
            from migu_api.config import PROD as prof
        exp = (catalog.pair(env_name, "free_fixture"), catalog.triple(env_name, "paid"))
        act = (prof.fixture_free_product, prof.paid_test_product)
        mark = "✓" if exp == act else "✗"
        if exp != act:
            ok = False
        print(f"  {mark} config 派生一致：清单={exp} / config={act}")

    if args.online:
        ok = _online_check() and ok

    print("\n结论：" + ("✓ 清单健康" if ok else "✗ 有问题，见上面"))
    return 0 if ok else 1


def _online_check() -> bool:
    """联网核对：清单里登记的 SKU 在环境上是否还存在、未过期、有库存。"""
    try:
        from migu_api.client import MiguClient
    except Exception as exc:  # noqa: BLE001
        print(f"\n[在线核对] 跳过：无法 import 客户端（{exc}）")
        return True
    try:
        client = MiguClient()
        client.login()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[在线核对] 跳过：C 端登录失败（{exc}）")
        print("  提示：./run.sh login --harvest 可从浏览器取登录态")
        return True

    cat = catalog.load()
    print("\n[在线核对] 逐个商品拉 SKU 列表比对：")
    cache: dict = {}
    bad = 0
    for t in cat.targets():
        if t.product_id not in cache:
            try:
                cache[t.product_id] = client.get_sku_list(t.product_id)
            except Exception as exc:  # noqa: BLE001
                cache[t.product_id] = None
                print(f"  ✗ productId={t.product_id} 拉取失败：{str(exc)[:80]}")
                bad += 1
                continue
        skus = cache[t.product_id] or []
        hit = next((s for s in skus if str(s.get("skuId")) == str(t.sku_id)), None)
        if hit is None:
            print(f"  ✗ [{t.project_key}] skuId={t.sku_id} 在环境上已不存在")
            bad += 1
        elif hit.get("expired") or int(hit.get("count") or 0) <= 0:
            print(f"  ✗ [{t.project_key}] skuId={t.sku_id} 已过期/无库存"
                  f"（expired={hit.get('expired')} count={hit.get('count')}）")
            bad += 1
        else:
            print(f"  ✓ [{t.project_key}] skuId={t.sku_id} 可用"
                  f"（库存 {hit.get('count')}，价 {hit.get('price')}）")
    if bad:
        print(f"  → {bad} 条登记已失效，请在 B 端确认后用 "
              "./run.sh catalog add --update 更新清单")
    return bad == 0


# ---------------------------------------------------------------- add

def _parse_sku(text: str) -> dict:
    """skuId:价格[:名称]"""
    parts = text.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(f"--sku 格式应为 skuId:价格[:名称]，收到 {text!r}")
    sku = {"key": "sku", "skuId": int(parts[0]), "price": parts[1], "usages": []}
    if len(parts) >= 3:
        sku["name"] = parts[2]
    return sku


def cmd_add(args) -> int:
    path = catalog.catalog_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    envs = raw.setdefault("environments", {})
    env = envs.setdefault(args.env, {"storeId": "", "projects": []})
    projects = env.setdefault("projects", [])

    existing = next((p for p in projects if str(p.get("key")) == args.key), None)
    if existing and not args.update:
        print(f"项目 {args.key} 已存在（加 --update 覆盖）。当前："
              f"productId={existing.get('productId')} status={existing.get('status')}")
        return 1

    prof = {
        "seat": bool(args.seat),
        "realName": args.real_name,
        "delivery": args.delivery,
        "deliveryType": args.delivery_type or [],
    }
    events = []
    if args.sku:
        events.append({
            "key": args.event_key,
            "eventId": args.event_id,
            "name": args.event_name or args.event_key,
            "maxVerifyCount": args.max_verify,
            "skus": [_parse_sku(s) for s in args.sku],
        })

    entry = {
        "key": args.key,
        "name": args.name or args.key,
        "productId": args.product_id,
        "status": args.status,
        "note": args.note or "",
        "profile": prof,
        "usages": args.usage or [],
        "events": events,
    }

    if existing:
        existing.clear()
        existing.update(entry)
        action = "更新"
    else:
        projects.append(entry)
        action = "新增"

    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    catalog.reload()
    print(f"✓ 已{action}项目 {args.key}（{args.env}）：{entry['name']}")
    print(f"  清单文件 {path}")
    if entry["productId"] is None:
        print("  ⚠️ 没给 --product-id，该项目仍不可下单（status 建议用 planned）。")
    elif not entry["events"]:
        print("  ⚠️ 没给 --sku，该项目没有可下单票档 → 白名单不会放行。")
    else:
        for t in catalog.load(args.env).targets():
            if t.project_key == args.key:
                print(f"  → 可下单：productId={t.product_id} skuId={t.sku_id} "
                      f"({t.price}元)")
    return 0


# ---------------------------------------------------------------- drift（漂移巡检）

def _drift_env(env_name: str, deep: bool, quiet: bool = False) -> bool:
    """巡检单个环境：清单登记的「项目-场次-票档」跟环境实况是否还一致。

    分两层，失败原因能一眼分清是"清单过期"还是"环境被改了"：
      L1（默认，只读 C 端接口，**不需要登录态**）
        · 项目还在不在（拉得到商品详情吗）
        · 登记的每个 skuId 还在不在、有没有过期/无库存、价格是否与登记一致
        · 拉出来的 sku 里有没有"清单没登记"的（提示，不算失败）
      L2（--deep，需要 B 端会话）
        · 项目/场次是否还在，场次名与登记的 key 对不对得上
        · 票档数量是否变化
    """
    import os as _os

    if _os.environ.get("MIGU_ENV", "test").lower() != env_name:
        # 正常情况下不会走到这（换环境由 cmd_drift 起子进程），这里只是兜底
        _os.environ["MIGU_ENV"] = env_name
        for mod in list(sys.modules):
            if mod.startswith("migu_api"):
                del sys.modules[mod]
    from migu_api import catalog as cat_mod  # noqa: E402

    def log(*a):
        if not quiet:
            print(*a)

    try:
        cat = cat_mod.load(env_name)
    except cat_mod.CatalogError as exc:
        print(f"[{env_name}] ✗ 清单加载失败：{exc}")
        return False

    log(f"[{env_name}] 清单版本 v{cat.version}（更新 {cat.updated}）"
        f" storeId={cat.store_id} · 项目 {len(cat.projects)} 个")

    from migu_api.client import MiguClient
    client = MiguClient()
    problems: list[str] = []
    sku_cache: dict = {}
    prod_cache: dict = {}

    for p in cat.projects:
        if p.product_id is None:
            log(f"  · {p.key} 未登记 productId（status={p.status}），跳过")
            continue
        pid = p.product_id
        if pid not in prod_cache:
            try:
                prod_cache[pid] = client.get_product_detail(pid)
                sku_cache[pid] = client.get_sku_list(pid)
            except Exception as exc:  # noqa: BLE001
                prod_cache[pid] = None
                sku_cache[pid] = None
                log(f"  ✗ {p.key} productId={pid} 环境上拉不到（{str(exc)[:70]}）")
                problems.append(f"{p.key} 项目不存在/不可访问")
                continue
        detail, skus = prod_cache[pid], sku_cache[pid] or []
        live = {str(s.get("skuId")): s for s in skus}
        name = str(detail.get("productName") or "")
        name_ok = (not p.name) or (name == p.name)
        tag = "✓" if name_ok else "!"
        log(f"  {tag} {p.key} productId={pid} 「{name}」 环境票档 {len(skus)} 个")
        if not name_ok:
            log(f"      ! 项目名与清单登记不一致：清单=「{p.name}」")

        targets = [t for t in cat.targets() if t.project_key == p.key]
        if not targets:
            log("      · 清单没登记票档（不可下单）")
            continue
        for t in targets:
            hit = live.get(str(t.sku_id))
            if hit is None:
                log(f"      ✗ 场次 {t.event_key} 的 skuId={t.sku_id} 环境上已不存在")
                problems.append(f"{p.key}/{t.event_key} skuId={t.sku_id} 不存在")
                continue
            price = hit.get("price")
            price_ok = price is None or abs(float(price) - float(t.price)) < 1e-6
            count = int(hit.get("count") or 0)
            expired = bool(hit.get("expired"))
            if expired:
                log(f"      ✗ skuId={t.sku_id} 已过期")
                problems.append(f"{p.key}/{t.event_key} skuId={t.sku_id} 已过期")
            elif count <= 0:
                log(f"      ! skuId={t.sku_id} 库存为 0（{t.price}元）——需在 B 端补库存")
                problems.append(f"{p.key}/{t.event_key} skuId={t.sku_id} 售罄")
            elif not price_ok:
                log(f"      ✗ skuId={t.sku_id} 价格漂移：清单={t.price} 环境={price}")
                problems.append(f"{p.key}/{t.event_key} skuId={t.sku_id} 价格漂移")
            else:
                log(f"      ✓ {t.event_key} skuId={t.sku_id} {t.price}元 库存{count}")

        registered = {str(t.sku_id) for t in targets}
        extra = sorted(set(live) - registered, key=lambda x: int(x))
        if extra:
            log(f"      · 环境上还有 {len(extra)} 个未登记的票档（skuId={','.join(extra[:8])}"
                f"{'…' if len(extra) > 8 else ''}）——要纳入下单范围就 catalog add --update")

        # 「未开售场次」必须真的没开售：清单约定 key=not_started 的场次，其票档在 C 端
        # 应带**未来的 fixedSaleTime**。没有 = 克隆时把开售时间窗丢了 → 变成"现在就能买"，
        # 既偏离 1:1 复刻，也让「未开售不可下单」这条负向用例无从覆盖。
        now_ms = int(time.time() * 1000)
        for ev_key in sorted({t.event_key for t in targets}):
            if ev_key != "not_started":
                continue
            ev_skus = [live.get(str(t.sku_id)) for t in targets if t.event_key == ev_key]
            ev_skus = [s for s in ev_skus if s]
            if not ev_skus:
                continue
            starts = []
            for s in ev_skus:
                try:
                    add = json.loads(s.get("addition") or "{}")
                except (TypeError, ValueError):
                    add = {}
                starts.append(add.get("fixedSaleTime"))
            future = [x for x in starts if isinstance(x, (int, float)) and x > now_ms]
            if not future:
                log(f"      ✗ 未开售场次（{ev_key}）的票档没有未来的 fixedSaleTime"
                    f"（实测 {starts}）—— 等于已开售，负向用例无法覆盖")
                problems.append(f"{p.key}/{ev_key} 未开售时间窗缺失")
            else:
                log(f"      ✓ 未开售场次（{ev_key}）开售时间 {min(future)}（晚于当前）")

    # ---------------- L2：B 端视角（场次/配置）----------------
    if deep:
        from migu_api.config import b_env as _b_env

        # 兜底：B 端环境必须在 import 前就设对。拿测试店去核对生产项目会得到
        # 「场次不存在」这类**假阳性**，反过来更糟——会给出**假绿**。
        try:
            b = None
            if _b_env.name != env_name:
                log(f"      ✗ B 端环境不匹配：MIGU_B_ENV={_b_env.name}，"
                    f"核对 {env_name} 清单应为 {env_name}（跳过 B 端巡检）")
                problems.append(f"B 端环境不匹配（MIGU_B_ENV={_b_env.name}≠{env_name}）")
            else:
                from migu_api.badmin import BAdminClient
                b = BAdminClient()
        except Exception as exc:  # noqa: BLE001
            # --deep 是显式要求的：拿不到客户端要算失败，不然会是"假绿"
            log(f"      ✗ B 端巡检无法进行：拿不到 B 端客户端（{str(exc)[:70]}）")
            problems.append(f"B 端巡检未执行（{str(exc)[:50]}）")
            b = None
        if b is not None:
            log(f"  [B 端巡检] storeId={b.store_id}")
            for p in cat.projects:
                if p.product_id is None:
                    continue
                pid = p.product_id
                try:
                    rows = b._request(
                        "GET", f"/v3/shop/product/{pid}/selfBuildActivity/events",
                        params={"storeId": b.store_id})
                    rows = rows.get("events") or rows.get("data") or []
                    live_events = {str(r.get("productEventId")): r.get("name") for r in rows}
                    cfg = b._request("GET", f"/v3/shop/product/{pid}/selfBuildActivity",
                                     params={"storeId": b.store_id})
                except Exception as exc:  # noqa: BLE001
                    # 同上：显式要求查 B 端却读不到，必须算失败（否则漏报漂移）
                    log(f"      ✗ {p.key} B 端读取失败：{str(exc)[:70]}")
                    problems.append(f"{p.key} B 端读取失败")
                    continue
                seat_live = bool(cfg.get("supportSelectSeat"))
                real_live = cfg.get("needRealNameInfo")
                # 场次级「核销次数」另查一次：上面的 v3 selfBuildActivity 接口不返回该字段，
                # 只有 /v1/selfProduct/events/list 返回。注意**有的环境根本没有这个字段**
                # （生产 B 端 events/edit 也没有 maxVerifyCount）→ 说明"多次核销"的
                # **配置能力**没发布，所有场次实际恒为 1 次。
                try:
                    ev_rows = b._request(
                        "GET", "/v1/selfProduct/events/list",
                        params={"storeId": b.store_id, "productId": pid,
                                "pageIndex": 1, "pageSize": 100})
                    ev_rows = ev_rows.get("data") or []
                except Exception as exc:  # noqa: BLE001
                    log(f"      ✗ {p.key} 核销次数读取失败：{str(exc)[:70]}")
                    problems.append(f"{p.key} 核销次数读取失败")
                    ev_rows = []
                verify_live = {}
                for r in ev_rows:
                    eid = r.get("id") if r.get("id") is not None else r.get("eventId")
                    if eid is not None:
                        verify_live[str(eid)] = r.get("maxVerifyCount")
                # 按场次去重（清单是 sku 粒度的，一个场次有多条票档 → 别重复打印）
                seen_events: set = set()
                for t in [x for x in cat.targets() if x.project_key == p.key]:
                    if t.event_id is None or t.event_id in seen_events:
                        continue
                    seen_events.add(t.event_id)
                    if str(t.event_id) not in live_events:
                        log(f"      ✗ {p.key} 场次 eventId={t.event_id}（{t.event_key}）"
                            f"在 B 端已不存在")
                        problems.append(f"{p.key}/{t.event_key} 场次不存在")
                    else:
                        log(f"      ✓ {p.key} 场次 {t.event_key} eventId={t.event_id}"
                            f" 「{live_events[str(t.event_id)]}」")
                        # 核销次数核对（清单登记了才比，避免给没登记的环境报假红）
                        if t.max_verify_count is not None and str(t.event_id) in verify_live:
                            live_v = verify_live[str(t.event_id)]
                            if live_v is None:
                                if int(t.max_verify_count) != 1:
                                    log(f"      ✗ {p.key}/{t.event_key} 清单登记 "
                                        f"maxVerifyCount={t.max_verify_count}，"
                                        f"但环境不提供该配置（实际恒为 1）"
                                        f" —— 多次核销的配置能力在此环境未发布")
                                    problems.append(
                                        f"{p.key}/{t.event_key} 核销次数不可配置"
                                        f"（清单={t.max_verify_count} 实际=1）")
                            elif int(live_v) != int(t.max_verify_count):
                                log(f"      ✗ {p.key}/{t.event_key} 核销次数漂移："
                                    f"清单={t.max_verify_count} 环境={live_v}")
                                problems.append(f"{p.key}/{t.event_key} 核销次数漂移")
                if p.seat is not None and seat_live != bool(p.seat):
                    log(f"      ✗ {p.key} 选座配置漂移：清单={p.seat} 环境={seat_live}")
                    problems.append(f"{p.key} 选座配置漂移")
                if p.real_name is not None and real_live is not None \
                        and int(real_live) != int(p.real_name):
                    log(f"      ✗ {p.key} 实名档位漂移：清单={p.real_name} 环境={real_live}")
                    problems.append(f"{p.key} 实名档位漂移")

    if problems:
        print(f"[{env_name}] ✗ 发现 {len(problems)} 处漂移：")
        for x in problems:
            print(f"      - {x}")
        print(f"      → 修完 B 端后用 ./run.sh catalog add --env {env_name} … --update 更新清单")
        return False
    log(f"[{env_name}] ✓ 清单与环境一致")
    return True


def cmd_drift(args) -> int:
    cur = os.environ.get("MIGU_ENV", "test").lower()
    cur_b = os.environ.get("MIGU_B_ENV", cur).lower()
    envs = ["test", "prod"] if (args.all or args.env == "all") else [args.env or cur]
    ok = True
    for name in envs:
        # 两个环境变量都在 import 时固化：
        #   MIGU_ENV   决定 C 端（L1）
        #   MIGU_B_ENV 决定 B 端（L2，只在 --deep 时用到）
        # 任一不匹配都得换进程，否则会"用测试店核对生产清单"（假绿/假阳性）。
        same = (name == cur) and (not args.deep or name == cur_b)
        if same:
            ok = _drift_env(name, args.deep) and ok
            continue
        print(f"\n--- 换进程巡检 [{name}]（环境在 import 时固化）---", flush=True)
        cmd = [sys.executable, str(Path(__file__).resolve()), "drift", "--env", name,
               "--no-summary"]
        child_env = {**os.environ, "MIGU_ENV": name}
        if args.deep:
            cmd.append("--deep")
            child_env["MIGU_B_ENV"] = name      # ★ B 端必须跟着一起切
        rc = subprocess.call(cmd, env=child_env)
        ok = (rc == 0) and ok
    if getattr(args, "no_summary", False):
        return 0 if ok else 1
    if len(envs) > 1:
        print("\n结论：" + ("✓ 两端清单都跟环境一致" if ok else "✗ 有漂移，见上面"))
    else:
        print("\n结论：" + ("✓ 清单与环境一致，无需维护" if ok else "✗ 有漂移，见上面"))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="catalog", description="测试专用商品清单管理（C 端下单白名单）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出清单")
    p.add_argument("--env", default=None, help="test / prod（默认取 MIGU_ENV）")
    p.add_argument("--status", default=None, choices=["ready", "rework", "planned"])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看某个项目的完整登记")
    p.add_argument("project")
    p.add_argument("--env", default=None)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("find", help="按条件查找可下单目标")
    p.add_argument("--env", default=None)
    p.add_argument("--project")
    p.add_argument("--event")
    p.add_argument("--sku")
    p.add_argument("--usage", action="append")
    p.add_argument("--seat", dest="seat", action="store_true", default=None)
    p.add_argument("--no-seat", dest="seat", action="store_false")
    p.add_argument("--real-name", type=int, choices=[0, 1, 2])
    p.add_argument("--delivery", choices=["eticket", "paper", "idcard"])
    p.add_argument("--price")
    p.add_argument("--max-price")
    p.add_argument("--min-verify", type=int)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("check", help="清单自检（默认离线）")
    p.add_argument("--online", action="store_true",
                   help="再联网核对清单里的 SKU 是否还能下单")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("drift", help="漂移巡检：清单 vs 环境实况（CI 每日跑）")
    p.add_argument("--env", default=None, choices=["test", "prod", "all"],
                   help="巡检哪个环境，all=两端都查（默认取 MIGU_ENV）")
    p.add_argument("--all", action="store_true", help="等价于 --env all")
    p.add_argument("--deep", action="store_true",
                   help="再查 B 端（场次数/选座/实名配置），需要有效 B 端会话")
    p.add_argument("--no-summary", dest="no_summary", action="store_true",
                   help="（内部用）不打印结尾结论")
    p.set_defaults(func=cmd_drift)

    p = sub.add_parser("add", help="追加/更新一个项目")
    p.add_argument("--env", default="test")
    p.add_argument("--key", required=True, help="项目编号，如 P08")
    p.add_argument("--name")
    p.add_argument("--product-id", type=int)
    p.add_argument("--status", default="ready",
                   choices=["ready", "rework", "planned"])
    p.add_argument("--note", default="")
    p.add_argument("--seat", action="store_true")
    p.add_argument("--real-name", type=int, choices=[0, 1, 2], default=0)
    p.add_argument("--delivery", choices=["eticket", "paper", "idcard"])
    p.add_argument("--delivery-type", type=int, nargs="*", default=[],
                   help="后端取值：电子票 4 / 快递 1 / 自取 3 / 身份证件 11")
    p.add_argument("--usage", action="append", help="可重复，如 --usage seat --usage idcard")
    p.add_argument("--event-key", default="regular")
    p.add_argument("--event-id", type=int)
    p.add_argument("--event-name")
    p.add_argument("--max-verify", type=int)
    p.add_argument("--sku", action="append",
                   help="可重复，格式 skuId:价格[:名称]")
    p.add_argument("--update", action="store_true", help="已存在同 key 时覆盖")
    p.set_defaults(func=cmd_add)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except catalog.CatalogError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
