# -*- coding: utf-8 -*-
"""Summarize the latest Allure result directory for quick triage."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = ROOT / "allure-results"
DEFAULT_REPORT = ROOT / "allure-report"


def _load_results(result_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(result_dir.glob("*-result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        labels: dict[str, list[str]] = collections.defaultdict(list)
        for label in data.get("labels", []):
            labels[label.get("name", "")].append(label.get("value", ""))
        rows.append({
            "name": data.get("name") or data.get("fullName") or path.name,
            "status": data.get("status") or "unknown",
            "duration": max(0, (data.get("stop", 0) - data.get("start", 0)) / 1000),
            "message": ((data.get("statusDetails") or {}).get("message") or "").strip(),
            "suite": (labels.get("suite") or ["<none>"])[0],
            "tags": sorted(labels.get("tag", [])),
            "attachments": data.get("attachments", []),
        })
    return rows


def _one_line(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _report_time(report_dir: Path) -> str | None:
    path = report_dir / "widgets" / "summary.json"
    if not path.exists():
        return None
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
        start = dt.datetime.fromtimestamp(summary["time"]["start"] / 1000)
        stop = dt.datetime.fromtimestamp(summary["time"]["stop"] / 1000)
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return f"{start:%Y-%m-%d %H:%M:%S} -> {stop:%Y-%m-%d %H:%M:%S}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS,
                        help="Allure result directory, default: ./allure-results")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT,
                        help="Generated Allure report directory, default: ./allure-report")
    parser.add_argument("--slow", type=int, default=10,
                        help="How many slowest cases to print")
    args = parser.parse_args()

    rows = _load_results(args.results)
    if not rows:
        raise SystemExit(f"No Allure result files found in {args.results}")

    print(f"Allure results: {args.results}")
    report_time = _report_time(args.report)
    if report_time:
        print(f"Report time: {report_time}")
    print()

    total = len(rows)
    counts = collections.Counter(row["status"] for row in rows)
    print("Status:")
    for status in ("passed", "failed", "broken", "skipped", "unknown"):
        if counts[status]:
            print(f"  {status:7} {counts[status]}")
    print(f"  total   {total}")

    print("\nBy suite:")
    for suite in sorted({row["suite"] for row in rows}):
        suite_counts = collections.Counter(row["status"] for row in rows if row["suite"] == suite)
        bits = ", ".join(f"{key}={suite_counts[key]}" for key in sorted(suite_counts))
        print(f"  {suite}: {bits}")

    problem_rows = [row for row in rows if row["status"] in {"failed", "broken"}]
    if problem_rows:
        print("\nFailed / broken:")
        for row in sorted(problem_rows, key=lambda item: (item["status"], -item["duration"])):
            tags = ",".join(row["tags"]) or "-"
            print(f"  [{row['status']}] {row['duration']:.1f}s {row['name']} ({tags})")
            if row["message"]:
                print(f"    {_one_line(row['message'])}")

    skipped = [row for row in rows if row["status"] == "skipped"]
    if skipped:
        print("\nSkipped:")
        for row in sorted(skipped, key=lambda item: item["name"]):
            print(f"  {row['name']}")
            if row["message"]:
                print(f"    {_one_line(row['message'])}")

    print("\nSlowest:")
    for row in sorted(rows, key=lambda item: -item["duration"])[:args.slow]:
        print(f"  {row['duration']:.1f}s {row['status']:7} {row['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
