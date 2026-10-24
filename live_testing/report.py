# -*- coding: utf-8 -*-
"""report.py —— 存活测试的汇总产物。

pytest/Allure 里已经能逐条看结果；这里额外落一份**机器可读的 JSON 汇总**，
方便 CI 归档、做趋势对比，以及快速回答“哪些接口 5xx / 401 / 404”。

输出：
    artifacts/live/live-summary-<时间戳>[-worker].json
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .classify import category as _category
from .classify import is_failure_from_row as _is_failure_from_row
from .client import LiveResult
from .config import ROOT, LiveSettings


def _known_reason(result: LiveResult, settings: LiveSettings) -> str:
    for key in (f"{result.method} {result.path}", result.path):
        reason = settings.known_issues.get(key)
        if reason:
            return reason
    return ""


def _row(result: LiveResult, settings: LiveSettings) -> dict:
    return {
        "service": result.service,
        "method": result.method,
        "path": result.path,
        "risk": result.risk,
        "known_issue": _known_reason(result, settings),
        "status": result.status_code,
        "code": result.business_code,
        "message": result.message,
        "category": _category(result, settings),
        "elapsed_ms": result.elapsed_ms,
        "error": result.error,
        "url": result.url,
        "content_type": result.content_type,
        "fallback_tried": result.fallback_tried,
        "fallback_detail": result.fallback_detail,
    }


class LiveRecorder:
    def __init__(self, settings: LiveSettings, worker: str = "main"):
        self.settings = settings
        self.worker = worker
        self.started = time.time()
        self.rows: list[dict] = []

    def add(self, result: LiveResult) -> None:
        self.rows.append(_row(result, self.settings))

    def summary(self) -> dict:
        categories = Counter(row["category"] for row in self.rows)
        by_service = Counter(row["service"] for row in self.rows)
        known = [row for row in self.rows if row.get("known_issue")]
        failures = [row for row in self.rows
                    if not row.get("known_issue")
                    and _is_failure_from_row(row, self.settings)]
        warnings = [row for row in self.rows
                    if not row.get("known_issue")
                    and row["category"] in {"auth_4xx", "not_found_404",
                                            "redirect_3xx", "html_2xx", "client_4xx",
                                            "business_data_5xx", "business_write_5xx"}]
        slowest = sorted(self.rows, key=lambda r: r["elapsed_ms"], reverse=True)[:10]
        return {
            "env": self.settings.env,
            "worker": self.worker,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(time.time() - self.started, 1),
            "totals": {
                "executed": len(self.rows),
                "failed": len(failures),
                "known_issues": len(known),
                "warnings": len(warnings),
            },
            "by_category": dict(sorted(categories.items())),
            "by_service": dict(sorted(by_service.items())),
            "failures": failures,
            "known_issues": known,
            "warnings": warnings,
            "slowest": slowest,
        }

    def write(self, config=None) -> Path:
        summary = self.summary()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "" if self.worker in {"", "main"} else f"-{self.worker}"
        out_dir = ROOT / "artifacts" / "live"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"live-summary-{stamp}{suffix}.json"
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._print(summary, path, config)
        return path

    def _print(self, summary: dict, path: Path, config=None) -> None:
        reporter = None
        if config is not None:
            reporter = config.pluginmanager.getplugin("terminalreporter")

        def line(text: str = "") -> None:
            if reporter is not None:
                reporter.write_line(text)
            else:
                print(text)

        totals = summary["totals"]
        line()
        line(f"=== 存活测试汇总（env={summary['env']}, worker={summary['worker']}）===")
        line(f"执行 {totals['executed']} 条，失败 {totals['failed']} 条，"
             f"已知问题 {totals['known_issues']} 条，"
             f"告警 {totals['warnings']} 条，耗时 {summary['duration_s']}s")
        line("分类：" + ", ".join(f"{k}={v}" for k, v in summary["by_category"].items()))
        if summary["failures"]:
            line(f"失败明细（{len(summary['failures'])}）：")
            for row in summary["failures"][:20]:
                detail = row["error"] or (
                    f"HTTP {row['status']} code={row['code']} {row['message']}"
                )
                line(f"  [{row['service']}] {row['method']} {row['path']} -> {detail}")
            if len(summary["failures"]) > 20:
                line(f"  ... 其余 {len(summary['failures']) - 20} 条见 JSON")
        line(f"汇总文件：{path}")
        line()


