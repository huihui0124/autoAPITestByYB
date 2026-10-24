# -*- coding: utf-8 -*-
"""
cli.py —— casegen 命令入口（`./run.sh casegen …`）

    template                          打印模版格式规范（列/宽度/底色/合并/约定）
    build   <spec.md> [-o out.xlsx]   规格 → 规范用例 Excel（生成后自动逐格校验）
    check   <spec.md|*.xlsx>          校验规格或已生成的 Excel 是否符合模版
    fmt-diff <file.xlsx> [--ref R]     与基准模版做**外观指纹**比对（体检单份文件）
    fmt-scan <dir> [--ref R]          批量体检目录下的存量用例（哪些格式有分叉）
    assets  <dir> [--out idx.json]    读存量资产：覆盖地图 + 偏差清单
                                      ★默认只认「含结果」版（同族唯一权威源），--all 关掉
    refs    <dir> [-q 关键词] [-m 模块]  查已有用例（生成新用例前查重）
    plan    <spec.md> [--env test]    把「环境要求」匹配到商品清单里的项目
    provision <spec.md> [--env test]  清单里缺的项目 → 在 B 端建 + 登记进清单（默认 dry-run）
    inventory                        实际建好的商品 →《测试商品建设清单》xlsx（两环境实测，只读）
                                     子命令：all（默认）| snapshot --env X -o f.json | build --test f --prod f

退出码：0 全部 OK；1 有问题（CI 里可直接当闸门）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import assets as assets_mod
from . import factory, fmtdiff, resolve, template
from .spec import SpecError, check_spec, parse_spec

# 默认基准模版（user 的 930 母版；用 MIGU_CASE_REF 覆盖）
DEFAULT_REF = os.environ.get(
    "MIGU_CASE_REF",
    "/Users/yubing/Documents/haomai/haomai-migu/930测试用例V1.2.0/"
    "咪咕赛事票务系统一期V1.2.0测试用例（930）.xlsx",
)

# 存量资产目录 + 权威版本标记（2026-09-30 user 定：以「含结果」为准）
DEFAULT_ASSET_DIR = os.environ.get(
    "MIGU_CASE_ASSETS",
    "/Users/yubing/Documents/haomai/haomai-migu/930测试用例V1.2.0",
)
AUTH_MARK = os.environ.get("MIGU_CASE_AUTH_MARK", assets_mod.AUTH_MARK)


def _env_default() -> str:
    return os.environ.get("MIGU_ENV", "test")


# ------------------------------------------------------------------ 子命令

def cmd_template(args) -> int:
    print(template.describe_template())
    return 0


def _load(path: str):
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        raise SpecError(f"{path} 是 Excel；校验 Excel 请用 `casegen check {path}`")
    return parse_spec(p)


def cmd_build(args) -> int:
    suite = _load(args.spec)
    out = args.out or str(Path(args.spec).with_suffix("")) + ".xlsx"
    path = template.write_workbook(suite, out, sheet_name=args.sheet)
    n_steps = sum(len(c.steps) for c in suite.cases)
    print(f"已生成 {path}")
    print(f"  用例 {len(suite.cases)} 条 / 步骤行 {n_steps} 行 / "
          f"{len(template.TEMPLATE)} 列（A–{chr(64 + len(template.TEMPLATE))}）"
          f" / 工作表「{suite.sheet_name}」")
    problems = template.validate_workbook(path)
    if problems:
        print(f"\n✗ 格式校验未通过（{len(problems)} 项）：")
        for p in problems[:20]:
            print(f"   - {p}")
        return 1
    print("✓ 格式校验通过（列/表头/底色/字体/边框/合并/步骤预期结果 逐格核对）")
    return 0


def cmd_check(args) -> int:
    target = Path(args.target)
    if target.suffix.lower() in (".xlsx", ".xlsm"):
        problems = template.validate_workbook(target)
        label = "Excel 格式"
    else:
        suite = parse_spec(target)
        problems = check_spec(suite, source=str(target))
        label = "规格"
        if not problems:
            print(f"✓ {label}自检通过：{len(suite.cases)} 条用例 / "
                  f"{sum(len(c.steps) for c in suite.cases)} 个步骤行")
            return 0
    if problems:
        print(f"✗ {label}校验未通过（{len(problems)} 项）：")
        for p in problems[:30]:
            print(f"   - {p}")
        return 1
    print(f"✓ {label}校验通过：{target}")
    return 0


def cmd_plan(args) -> int:
    suite = _load(args.spec)
    env = args.env or _env_default()
    rows = resolve.plan(suite, env)
    if not rows:
        print("规格里没有任何「环境要求」，无法定位项目。")
        return 1
    misses = 0
    print(f"环境要求 → 商品清单匹配（env={env}）")
    print("=" * 78)
    for raw, res, cases in rows:
        head = f"· {raw}"
        print(head)
        print(f"    用于用例：{', '.join(cases) if cases else '—'}")
        if res.ok:
            print(f"    ✓ 命中 {res.best.label}")
            continue
        d = factory.diagnose(env, raw)
        if d.action == "relabel":
            print(f"    △ 有同形态项目但标签不匹配 → 不必新建项目")
            print(f"      {d.detail}")
            print(f"      处理：./run.sh catalog add --env {env} --key "
                  f"{d.target_label.split('] ')[0].strip('[')} … --update 补标签")
        elif d.action == "create":
            misses += 1
            print(f"    ✗ 清单里没有这种形态 → 需要新建项目")
            print(f"      {d.detail}")
            if d.blueprint:
                print(f"      {d.blueprint.describe()}")
        else:
            misses += 1
            print(f"    ✗ 无法自动满足：{d.detail}")
    print("=" * 78)
    print(f"共 {len(rows)} 条环境要求：命中 {sum(1 for _, r, _ in rows if r.ok)} 条，"
          f"需新建 {misses} 条")
    return 1 if misses else 0


def cmd_provision(args) -> int:
    suite = _load(args.spec)
    env = args.env or _env_default()
    rows = resolve.plan(suite, env)
    todo = []
    for raw, res, cases in rows:
        d = factory.diagnose(env, raw)
        d.cases = cases
        todo.append((raw, res, d))

    need = [(r, d) for r, res, d in todo if d.action == "create"]
    print(f"环境要求 {len(todo)} 条：命中 {sum(1 for _, res, _ in todo if res.ok)}，"
          f"需新建 {len(need)}，"
          f"补标签 {sum(1 for _, _, d in todo if d.action == 'relabel')}，"
          f"建不了 {sum(1 for _, _, d in todo if d.action == 'unsupported')}")

    if not need:
        print("\n无需新建项目。")
        for raw, _, d in todo:
            if d.action == "relabel":
                print(f"  △ {raw}\n      {d.detail}")
            elif d.action == "unsupported":
                print(f"  ✗ {raw}\n      {d.detail}")
        return 0

    applied = []
    for raw, d in need:
        bp = d.blueprint
        if bp is None:
            continue
        print("-" * 78)
        print(f"为环境要求「{raw}」（用例 {', '.join(d.cases)}）建项目：")
        rec = factory.create_project(bp, apply=args.apply and args.yes)
        if not rec.get("dry_run"):
            factory.register_to_catalog(rec)
            applied.append(rec)

    if applied:
        print("=" * 78)
        print("已新建并登记：")
        for rec in applied:
            print(f"  {rec['key']} productId={rec['productId']} {rec['name']}")
        print("\n下一步：重新跑 plan 确认全部命中，再跑 catalog drift --deep 复核环境一致。")
    else:
        print("=" * 78)
        print("以上为 dry-run。确认无误后执行：\n"
              f"  ./run.sh casegen provision {args.spec}"
              + (f" --env {env}" if args.env else "") + " --apply --yes")
    return 0


def cmd_fmt_diff(args) -> int:
    ref = Path(args.ref or DEFAULT_REF)
    if not ref.exists():
        print(f"✗ 找不到基准模版：{ref}\n  用 --ref 指定，或设 MIGU_CASE_REF")
        return 1
    a, b, problems, info = fmtdiff.compare(ref, args.target)
    print(f"基准：{ref.name}")
    print(f"本次：{Path(args.target).name}")
    print("=" * 78)
    if problems:
        print(f"✗ 外观不一致（{len(problems)} 项）：")
        for p in problems:
            print(f"  - {p}")
    else:
        print("✓ 外观与基准一致（列宽/表头/底色/边框/合并/对齐/行高规则/默认值）")
    for i in info:
        print(f"  · 提示：{i}")
    return 1 if problems else 0


def cmd_fmt_scan(args) -> int:
    ref = Path(args.ref or DEFAULT_REF)
    root = Path(args.dir)
    files = sorted(p for p in root.rglob(args.glob)
                   if p.suffix.lower() in (".xlsx", ".xlsm")
                   and not p.name.startswith("~$"))
    if args.max_mb:
        big = [p for p in files if p.stat().st_size > args.max_mb * 1024 * 1024]
        files = [p for p in files if p not in big]
    print(f"基准：{ref.name}")
    print(f"范围：{root}（{len(files)} 份，忽略 > {args.max_mb}MB 的 {len(big) if args.max_mb else 0} 份）")
    print("=" * 78)
    ok, bad = 0, []
    for p in files:
        try:
            _, _, problems, _ = fmtdiff.compare(ref, p)
        except Exception as e:                       # 打不开的旧文件/截图版
            print(f"  ?  {p.name}  ← 读取失败：{type(e).__name__}: {str(e)[:60]}")
            continue
        if problems:
            bad.append((p, problems))
            print(f"  ✗  {p.name}")
            for pr in problems[:2]:
                print(f"       {pr.splitlines()[0][:110]}")
        else:
            ok += 1
            print(f"  ✓  {p.name}")
    print("=" * 78)
    print(f"一致 {ok} 份 / 有分叉 {len(bad)} 份")
    return 1 if bad else 0


def cmd_assets(args) -> int:
    root = args.dir or DEFAULT_ASSET_DIR
    only = None if args.all else (args.only or AUTH_MARK)
    res, dropped, replaced = assets_mod.scan_report(
        root, glob=args.glob, max_mb=args.max_mb, only=only)
    if only:
        print(f"权威源策略：只读文件名含「{only}」的文件（--all 可关掉）")
    if replaced:
        print(f"同族去重：{len(replaced)} 份被同族的「{AUTH_MARK}」版顶替")
        for a, b in replaced[:6]:
            print(f"  · 弃 {a.name}  ← 用 {b.name}")
    if dropped and not only:
        print(f"跳过 {len(dropped)} 份：")
        for p, why in dropped[:6]:
            print(f"  · {p.name}  （{why}）")
    print(assets_mod.summarize(res))
    if args.out:
        out = assets_mod.build_index(res, args.out, root=root,
                                     dropped=dropped, replaced=replaced)
        print(f"\n覆盖地图已写出：{out}")
    if args.fails:
        print("\n" + "=" * 78)
        print("标为「失败/不通过」的用例清单（只列出，不复核）：")
        n = 0
        for fa in res:
            for c in fa.fails:
                n += 1
                print(f"  [{n}] {c.case_id}  {c.module} | {c.title}")
                print(f"       来源：{fa.path.name}  结果：{c.result.strip()[:60]}")
        if not n:
            print("  （无）")
    return 0


def cmd_refs(args) -> int:
    """查已有用例（生成新用例前查重）。"""
    root = args.dir or DEFAULT_ASSET_DIR
    only = None if args.all else (args.only or AUTH_MARK)
    res, _, replaced = assets_mod.scan_report(
        root, glob=args.glob, max_mb=args.max_mb, only=only)
    ok = [a for a in res if not a.error]
    if not ok:
        print(f"✗ 在 {root} 里没读到可用的用例文件（当前筛选：{only or '不限'}）")
        return 1
    hits = assets_mod.find(ok, query=args.query or "", module=args.module or "",
                           limit=args.limit)
    scope = " · ".join(f"{a.path.name}:{len(a.cases)}条" for a in ok)
    print(f"权威语料（{len(ok)} 份）：{scope}")
    print(f"查询：关键词={args.query or '—'} 模块={args.module or '—'}")
    print("=" * 78)
    if not hits:
        print("无命中 —— 该场景在权威语料里没有，属于**新增覆盖**。")
        return 0
    by_file = {}
    for fa, c in hits:
        by_file.setdefault(fa.path.name, []).append(c)
    for fname, cases in by_file.items():
        print(f"\n【{fname}】{len(cases)} 条")
        for c in cases:
            print(f"  {c.case_id}  {c.priority:<3} {c.module} | {c.title[:60]}")
            if args.verbose:
                if c.precondition:
                    print(f"        前置：{c.precondition[:90]}")
                for a, e in c.steps[:4]:
                    print(f"        步：{a[:70]}")
                    if e:
                        print(f"        期：{e[:70]}")
    print("=" * 78)
    print(f"共命中 {len(hits)} 条（上限 {args.limit}）。生成新用例时请对照这份清单避免重复。")
    return 0


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="casegen", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("template", help="打印模版格式规范")
    p.set_defaults(func=cmd_template)

    p = sub.add_parser("build", help="规格 → 用例 Excel")
    p.add_argument("spec")
    p.add_argument("-o", "--out", help="输出 xlsx（默认与规格同名）")
    p.add_argument("--sheet", help="工作表名（默认取规格 front-matter 的 sheet）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("check", help="校验规格或 Excel 是否符合模版")
    p.add_argument("target")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("fmt-diff", help="与基准模版做外观指纹比对")
    p.add_argument("target", help="要体检的 xlsx")
    p.add_argument("--ref", help=f"基准模版（默认 {Path(DEFAULT_REF).name}）")
    p.set_defaults(func=cmd_fmt_diff)

    p = sub.add_parser("fmt-scan", help="批量体检目录下的存量用例格式")
    p.add_argument("dir")
    p.add_argument("--ref", help="基准模版")
    p.add_argument("--glob", default="*.xlsx", help="文件名模式（默认 *.xlsx，递归匹配）")
    p.add_argument("--max-mb", type=float, default=8.0, help="跳过大于该体积的文件（默认 8MB，0=不限）")
    p.set_defaults(func=cmd_fmt_scan)

    p = sub.add_parser("assets", help="读取存量用例资产（覆盖地图 / 偏差清单）")
    p.add_argument("dir", nargs="?", help=f"存量用例目录（默认 {DEFAULT_ASSET_DIR}）")
    p.add_argument("--glob", default="*.xlsx", help="文件名模式（默认 *.xlsx，递归）")
    p.add_argument("--max-mb", type=float, default=40.0,
                   help="跳过大于该体积的文件（默认 40MB——含结果版带截图；0=不限）")
    p.add_argument("--only", help=f"只读文件名含该子串的文件（默认「{AUTH_MARK}」）")
    p.add_argument("--all", action="store_true",
                   help="关掉权威源策略，读目录下全部文件（含纯用例版/冒烟自测版）")
    p.add_argument("--out", help="把覆盖地图写成 JSON")
    p.add_argument("--fails", action="store_true", help="列出标为失败/不通过的用例")
    p.set_defaults(func=cmd_assets)

    p = sub.add_parser("refs", help="查已有用例（生成新用例前查重）")
    p.add_argument("dir", nargs="?", help=f"存量用例目录（默认 {DEFAULT_ASSET_DIR}）")
    p.add_argument("-q", "--query", help="关键词（编号/标题/前置/步骤/预期/备注 全字段匹配）")
    p.add_argument("-m", "--module", help="限定功能模块（子串匹配）")
    p.add_argument("--glob", default="*.xlsx")
    p.add_argument("--max-mb", type=float, default=40.0)
    p.add_argument("--only", help=f"只读文件名含该子串的文件（默认「{AUTH_MARK}」）")
    p.add_argument("--all", action="store_true", help="关掉权威源策略")
    p.add_argument("--limit", type=int, default=60, help="最多展示多少条（默认 60）")
    p.add_argument("-v", "--verbose", action="store_true", help="附带前置条件与步骤")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("plan", help="环境要求 → 项目匹配报告")
    p.add_argument("spec")
    p.add_argument("--env", choices=["test", "prod"])
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("provision", help="缺的项目在 B 端建好并登记进清单")
    p.add_argument("spec")
    p.add_argument("--env", choices=["test", "prod"])
    p.add_argument("--apply", action="store_true", help="真的写（默认 dry-run）")
    p.add_argument("--yes", action="store_true", help="与 --apply 同时给才落笔（防手滑）")
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser("inventory",
                       help="实际建好的商品 →《测试商品建设清单》xlsx（两环境实测，只读）")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="透传给 inventory 子命令，如 `--only test -o /tmp/x.xlsx`")
    p.set_defaults(func=cmd_inventory)
    return ap


def cmd_inventory(args) -> int:
    from . import inventory
    return inventory.main(list(getattr(args, "rest", None) or []))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `inventory` 自带一套参数（-o/--only/…），在 argparse 之前截走，避免被主解析器吃掉
    if argv and argv[0] == "inventory":
        from . import inventory
        return inventory.main(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SpecError as e:
        print(f"✗ 规格有问题：{e}")
        return 1
    except factory.FactoryError as e:
        print(f"✗ 无法建项目：{e}")
        return 1
    except AssertionError as e:
        print(f"✗ {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
