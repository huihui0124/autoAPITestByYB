# -*- coding: utf-8 -*-
"""
onsale_ctl.py —— 「商品上架/下架」命令（B 端操作）

为什么需要它：
    在 B 端建好项目 ≠ C 端能下单。项目建完后还差**商品级上架**这一步，
    没上架时 C 端下单会报 `商品已下架`（makeOrder code=400），
    表现为核心链路用例成片失败，很像是框架坏了。

    接口（逆向自 B 端前端 p__store___layout.js / umi.js）：
        上架  POST /v1/shopTrade/product/{productId}/onSale/true?storeId={storeId}
        下架  POST /v1/shopTrade/product/{productId}/onSale/false?storeId={storeId}
    UI 的「保存并上架」= createSelfBuildProduct() 之后紧接 onSaleProduct()。

用法（也可走 ./run.sh onsale ...）：
    ./run.sh onsale list                     看当前环境每个项目的上架状态
    ./run.sh onsale list --env prod
    ./run.sh onsale on  --key P08            上架指定项目（可重复 --key）
    ./run.sh onsale on  --all                上架清单里全部项目
    ./run.sh onsale off --key P08            下架

安全：
    · 环境由 MIGU_B_ENV 决定（默认 test），只读 list 不写
    · env=prod 的**写操作**必须显式加 --yes（或 MIGU_ALLOW_PROD_WRITE=1）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migu_api import catalog  # noqa: E402


def _b_client(env_name: str):
    """B 端客户端。环境在 import 时固化，所以先设好 MIGU_B_ENV 再 import。"""
    os.environ["MIGU_B_ENV"] = env_name
    for mod in list(sys.modules):
        if mod.startswith("migu_api"):
            del sys.modules[mod]
    from migu_api.badmin import BAdminClient  # noqa: E402
    return BAdminClient()


def _projects(env_name: str, keys: list[str], want_all: bool) -> list[tuple[str, int]]:
    """从 catalog 取 (key, productId)：--all 取全部，--key 取指定。"""
    cat = catalog.load(env_name)
    out: list[tuple[str, int]] = []
    for p in cat.projects:
        if p.product_id is None:
            continue
        if want_all or (keys and p.key in keys):
            out.append((p.key, int(p.product_id)))
    if keys and not want_all:
        missing = set(keys) - {k for k, _ in out}
        if missing:
            print(f"✗ 清单里找不到这些项目（或没有 productId）：{', '.join(sorted(missing))}",
                  file=sys.stderr)
    return out


def _live_status(b, pids: list[int]) -> dict[int, dict]:
    """拉 B 端项目列表，取回 onSale 与场次在售统计（分页拉全）。"""
    found: dict[int, dict] = {}
    page = 1
    while True:
        lst = b._request("GET", "/v1/selfProduct/list",
                         params={"storeId": b.store_id, "pageIndex": page, "pageSize": 50})
        rows = lst.get("data") or []
        for r in rows:
            pid = int(r.get("productId"))
            if pid in pids:
                found[pid] = r
        total = int(lst.get("totalNum") or 0)
        if not rows or page * 50 >= total:
            break
        page += 1
    return found


def cmd_list(args) -> int:
    b = _b_client(args.env)
    targets = _projects(args.env, args.key, args.all or not args.key)
    pids = [pid for _, pid in targets]
    live = _live_status(b, pids)
    print(f"环境 {args.env} / storeId={b.store_id}：{len(targets)} 个项目")
    n_off = 0
    for key, pid in targets:
        r = live.get(pid)
        if r is None:
            print(f"  ? {key:<4} {pid}  列表里没找到")
            continue
        on = bool(r.get("onSale"))
        n_off += 0 if on else 1
        flag = "已上架" if on else "★未上架（C 端下单会报「商品已下架」）"
        print(f"  {'✓' if on else '✗'} {key:<4} {pid}  {flag}"
              f"  场次在售={r.get('onSaleEventSum')} 库存={r.get('onSaleTotalStockNum')}")
    if n_off:
        print(f"\n→ {n_off} 个项目未上架，修复：./run.sh onsale on --all --env {args.env}")
    return 0


def cmd_toggle(args) -> int:
    want_on = args.cmd == "on"
    if args.env == "prod" and not (args.yes or os.environ.get("MIGU_ALLOW_PROD_WRITE") == "1"):
        print("✗ 在生产环境做上架/下架属于写操作。确认请加 --yes"
              "（或 MIGU_ALLOW_PROD_WRITE=1）。", file=sys.stderr)
        return 3

    b = _b_client(args.env)
    targets = _projects(args.env, args.key, args.all)
    if not targets:
        print("✗ 没有要处理的项目（用 --all 或 --key P01）", file=sys.stderr)
        return 2

    live = _live_status(b, [pid for _, pid in targets])
    ok = True
    for key, pid in targets:
        cur = live.get(pid)
        if cur is None:
            print(f"  ? {key} {pid} 列表里没找到，跳过")
            ok = False
            continue
        if bool(cur.get("onSale")) == want_on:
            print(f"  · {key} {pid} 已经是{'上架' if want_on else '下架'}状态，跳过")
            continue
        r = b._request("POST", f"/v1/shopTrade/product/{pid}/onSale/{'true' if want_on else 'false'}",
                       params={"storeId": b.store_id})
        print(f"  ✓ {key} {pid} {'上架' if want_on else '下架'} → {json.dumps(r, ensure_ascii=False)[:60] or '{}'}")

    # 回读复核
    live2 = _live_status(b, [pid for _, pid in targets])
    bad = [(k, p) for k, p in targets if bool((live2.get(p) or {}).get("onSale")) != want_on]
    if bad:
        print(f"\n✗ 复核不通过：{', '.join(f'{k}({p})' for k, p in bad)}")
        return 1
    print(f"\n✓ 复核通过：{len(targets)} 个项目均为{'上架' if want_on else '下架'}状态")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="onsale", description="商品上架/下架（B 端）")
    ap.add_argument("cmd", choices=["list", "on", "off"], help="list=查看；on=上架；off=下架")
    ap.add_argument("--env", default=os.environ.get("MIGU_B_ENV", "test"),
                    choices=["test", "prod"], help="默认取 MIGU_B_ENV（再默认 test）")
    ap.add_argument("--key", action="append", default=[], help="项目编号，可重复，如 --key P08")
    ap.add_argument("--all", action="store_true", help="处理清单里的全部项目")
    ap.add_argument("--yes", action="store_true", help="生产环境写操作确认")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return cmd_list(args) if args.cmd == "list" else cmd_toggle(args)
    except catalog.CatalogError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
