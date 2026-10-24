# -*- coding: utf-8 -*-
"""generator.py —— OpenAPI → live_testing/cases/*.json

用法（在仓库根目录执行）：
    python -m live_testing.generator
    python -m live_testing.generator --check
    python -m live_testing.generator \
        --customer openAPI/haomai-customer-migu-in-use.json \
        --web      openAPI/haomai-web-migu-in-use.json \
        --out      live_testing/cases

生成物：
    live_testing/cases/customer.json   C 端逐接口用例
    live_testing/cases/web.json        B 端逐接口用例
    live_testing/cases/manifest.json   来源文件、sha256、数量统计

注意：生成器只读 OpenAPI，不发任何网络请求；登录态由运行时配置决定。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .openapi import SpecError, build_cases

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPECS = {
    "customer": ROOT / "openAPI" / "haomai-customer-migu-in-use.json",
    "web": ROOT / "openAPI" / "haomai-web-migu-in-use.json",
}


def _dump_json(path: Path, data) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _print_summary(service: str, manifest: dict, out_file: Path, size: int) -> None:
    counts = manifest["counts"]
    methods = ", ".join(f"{k}={v}" for k, v in counts["by_method"].items())
    risks = ", ".join(f"{k}={v}" for k, v in counts["by_risk"].items())
    tags = len(counts["by_tag"])
    print(f"  {service:<8} {counts['total']:>4} 条  ({methods}; {risks}; {tags} 个 tag)")
    print(f"           -> {out_file}  ({size / 1024:.1f} KB)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把过滤后的 OpenAPI 转成存活测试用例清单（只读，不发请求）",
    )
    parser.add_argument("--customer", default=str(DEFAULT_SPECS["customer"]),
                        help="C 端 OpenAPI 路径")
    parser.add_argument("--web", default=str(DEFAULT_SPECS["web"]),
                        help="B 端 OpenAPI 路径")
    parser.add_argument("--out", default=str(ROOT / "live_testing" / "cases"),
                        help="输出目录，默认 live_testing/cases")
    parser.add_argument("--check", action="store_true",
                        help="只解析并打印统计，不写文件")
    args = parser.parse_args(argv)

    specs = {"customer": Path(args.customer), "web": Path(args.web)}
    out_dir = Path(args.out)
    manifest = {
        "generator": "live_testing.generator",
        "description": "接口存活测试用例清单：只验证可达/非 5xx，不验证业务正确性",
        "services": {},
    }

    print("OpenAPI -> 存活用例清单")
    for service, spec_path in specs.items():
        if not spec_path.exists():
            print(f"  [跳过] {service}: 找不到 {spec_path}", file=sys.stderr)
            continue
        try:
            cases, service_manifest = build_cases(spec_path, service)
        except SpecError as exc:
            print(f"  [失败] {service}: {exc}", file=sys.stderr)
            return 2

        manifest["services"][service] = {
            key: service_manifest[key]
            for key in ("source", "source_sha256", "openapi", "server",
                        "deprecated_skipped", "counts")
        }

        if args.check:
            _print_summary(service, service_manifest, Path("<check>"), 0)
            continue

        case_file = out_dir / f"{service}.json"
        size = _dump_json(case_file, cases)
        _print_summary(service, service_manifest, case_file, size)

    if args.check:
        print("check 完成：未写文件。")
        return 0

    manifest_file = out_dir / "manifest.json"
    _dump_json(manifest_file, manifest)
    print(f"  manifest -> {manifest_file}")
    print("\n下一步：")
    print("  1) cp live_config.example.json live_config.json  # 填 hmcer / hmer 等 Cookie")
    print("  2) MIGU_LIVE=1 python -m pytest live_testing -m live -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
