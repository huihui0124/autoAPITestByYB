# -*- coding: utf-8 -*-
"""
spec.py —— 「用例规格（spec）」的读写：Markdown 表格 ⇄ Suite 对象

为什么用 Markdown 当中间层
    PRD / 技术文档本身是给人读的，从文档产出的用例也应该**先能用眼睛审一遍**再变成
    Excel。所以流水线是：

        需求文档/技术文档 ──(作者：人 或 智能体)──> 用例规格.md ──(本工具)──> 用例.xlsx
                                                     ↑
                                              可评审、可 diff、可版本管理

    规格里**一行就是一个操作步骤**（与目标 Excel 完全同构），所以翻译过程没有信息损失，
    也不会出现"一步内容被塞进一格"的糊在一起的写法。

规格文件格式
    ---
    用例文件: 邀请函-确认码与领取风控测试用例
    sheet: 邀请函-确认码
    来源文档: 邀请函功能技术设计文档(1).md
    ---

    | 用例编号 | 优先级 | 功能模块 | 用例标题 | 前置条件 | 操作步骤 | 预期结果 | 测试数据 | 可自动化 | 开发自测 | 备注 | 环境要求 |
    |---|---|---|---|---|---|---|---|---|---|---|---|
    | IFC_001 | P0 | 邀请函-确认码 | …… | …… | 1、打开邀请函列表 | 列表新增「确认码」列 |  | Y |  |  | seat=false, realName=0, delivery=eticket, usage=free_fixture |
    |  |  |  |  |  | 2、核对领取码与确认码 | 两码均随机、互不相同 |  |  |  |  |  |

    规则：
      · 第一行填「用例编号」→ 新起一条用例；「用例编号」留空 → 继续加步骤
      · 用例级列（编号/优先级/功能模块/标题/前置条件/可自动化/开发自测/备注/环境要求）
        只需在第一行填，后续步骤行留空即可
      · 「环境要求」不写进 Excel，用于在商品清单里找项目（见 resolve.py）
      · 测试结果 / 截图 列不出现在规格里，由 writer 留空
"""
from __future__ import annotations

import re
from pathlib import Path

from .template import Case, Step, Suite

# 规格表的列顺序（「环境要求」是工具用的额外列，不写进 Excel）
SPEC_HEADERS: tuple[str, ...] = (
    "用例编号", "优先级", "功能模块", "用例标题", "前置条件",
    "操作步骤", "预期结果", "测试数据", "可自动化", "开发自测", "备注", "环境要求",
)

_PRIORITIES = ("P0", "P1", "P2", "P3")


class SpecError(AssertionError):
    """规格文件本身写错了（结构不符 / 必填缺失）。"""


# ------------------------------------------------------------------ 读

def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells)


def _parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.lstrip().startswith("---"):
        return {}, text
    lines = text.lstrip().splitlines()
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, ""


def parse_spec(path: str | Path) -> Suite:
    """读规格 Markdown → Suite。结构错误直接抛 SpecError（绝不静默生成半成品）。"""
    text = Path(path).read_text(encoding="utf-8")
    meta, body = _parse_front_matter(text)
    stem = Path(path).stem

    suite = Suite(
        title=meta.get("用例文件") or stem,
        sheet_name=meta.get("sheet") or stem,
        source_doc=meta.get("来源文档") or "",
    )

    rows: list[list[str]] = []
    header_seen = False
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if not header_seen:
            if cells and cells[0] == "用例编号":
                if cells != list(SPEC_HEADERS):
                    missing = [h for h in SPEC_HEADERS if h not in cells]
                    if missing:
                        raise SpecError(f"规格表头缺少列：{', '.join(missing)}\n实际：{cells}")
                header_seen = True
            continue
        if _is_separator(cells):
            continue
        if not any(c for c in cells):
            continue
        rows.append(cells)

    if not header_seen:
        raise SpecError(f"{path} 里没找到规格表头（应以 | 用例编号 | 优先级 | … 开头）")
    if not rows:
        raise SpecError(f"{path} 里没有任何用例行")

    idx = {h: i for i, h in enumerate(SPEC_HEADERS)}

    def cell(cells: list[str], name: str) -> str:
        i = idx[name]
        return cells[i].strip() if i < len(cells) else ""

    for cells in rows:
        cid = cell(cells, "用例编号")
        if cid:                                   # 新用例
            case = Case(
                case_id=cid,
                priority=cell(cells, "优先级"),
                module=cell(cells, "功能模块"),
                title=cell(cells, "用例标题"),
                precondition=cell(cells, "前置条件"),
                auto=cell(cells, "可自动化"),
                dev_self=cell(cells, "开发自测"),
                note=cell(cells, "备注"),
                requires=cell(cells, "环境要求"),
            )
            suite.cases.append(case)
        if not suite.cases:
            raise SpecError(f"第一行数据行必须填「用例编号」（当前首格为「{cid or '空'}」）")
        suite.cases[-1].steps.append(Step(
            action=cell(cells, "操作步骤"),
            expect=cell(cells, "预期结果"),
            data=cell(cells, "测试数据"),
        ))

    check_spec(suite, source=str(path))
    return suite


def check_spec(suite: Suite, source: str = "") -> list[str]:
    """规格自检：编号唯一、必填齐全、每步都有预期结果。返回问题列表。"""
    problems: list[str] = []
    seen: set[str] = set()
    for case in suite.cases:
        where = f"{case.case_id or '(无编号)'}"
        if not case.case_id:
            problems.append("存在没有「用例编号」的用例")
        elif case.case_id in seen:
            problems.append(f"{where} 用例编号重复")
        seen.add(case.case_id)
        if case.priority not in _PRIORITIES:
            problems.append(f"{where} 优先级应为 P0/P1/P2，实际「{case.priority}」")
        for name, val in (("功能模块", case.module), ("用例标题", case.title),
                          ("前置条件", case.precondition)):
            if not val:
                problems.append(f"{where} 缺少「{name}」")
        if not case.steps:
            problems.append(f"{where} 没有任何操作步骤")
        for n, step in enumerate(case.steps, start=1):
            if not step.action.strip():
                problems.append(f"{where} 第 {n} 步「操作步骤」为空")
            if not step.expect.strip():
                problems.append(f"{where} 第 {n} 步「预期结果」为空"
                                "（约定：每个步骤行都要写自己的预期结果）")
        if not case.requires:
            problems.append(f"{where} 缺少「环境要求」—— 无法在清单里定位测试项目")
    if source:
        problems = [f"{Path(source).name}: {p}" for p in problems]
    return problems


# ------------------------------------------------------------------ 写

def dump_spec(suite: Suite, path: str | Path) -> Path:
    """把 Suite 写回规格 Markdown（便于人工评审 / 归档）。"""
    out = [f"# {suite.title}", ""]
    out += ["---",
            f"用例文件: {suite.title}",
            f"sheet: {suite.sheet_name}"]
    if suite.source_doc:
        out.append(f"来源文档: {suite.source_doc}")
    out += ["---", "",
            "| " + " | ".join(SPEC_HEADERS) + " |",
            "|" + "---|" * len(SPEC_HEADERS)]
    for case in suite.cases:
        for i, step in enumerate(case.steps):
            if i == 0:
                cells = [case.case_id, case.priority, case.module, case.title, case.precondition,
                         step.action, step.expect, step.data,
                         case.auto, case.dev_self, case.note, case.requires]
            else:
                cells = ["", "", "", "", "", step.action, step.expect, step.data, "", "", "", ""]
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p
