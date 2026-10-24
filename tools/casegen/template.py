# -*- coding: utf-8 -*-
"""
template.py —— 测试用例 Excel 的**唯一格式权威**（写入 + 校验）

为什么单独抽一个文件
    用例内容可以随便生成，但「长什么样」必须逐格稳定。历史上这里反复出错：
      · 列数/列序搞错（把 11 列的旧模版当标准，漏掉「测试结果」）
      · 表头底色 / 字体 / 边框写错
      · 合并列写错（哪些列按用例纵向合并）
      · 步骤行的「预期结果」该逐行写还是只写最后一行
    所以把格式收敛到本文件的 `TEMPLATE` 常量，写入与校验共用同一份定义 ——
    任何格式调整只改一处，且 `validate_workbook()` 能立刻发现产物跑偏。

格式来源（2026-09-30 逐格实测，user 确认）
    基准文件：咪咕赛事票务系统一期V1.2.0测试用例（930）.xlsx / 邀请函测试用例.xlsx
              （均为 A–P 共 16 列）
    模版选择：12 列核心 + 截图1–4（A–P）
    注意：haomai-migu/case.xlsx 是 **11 列旧版**（缺「测试结果」），不要拿它当基准。

16 列结构
    A 用例编号        B 优先级         C 功能模块       D 用例标题
    E 前置条件（可为空）  F 操作步骤     G 预期结果（可为空）  H 测试数据（可为空）
    I 测试结果        J 可自动化        K 开发自测（可为空）  L 备注（可为空）
    M–P 截图1–4

约定（2026-09-30 对 邀请函测试用例.xlsx / 930母版 逐格实测 + 直接读 XML 校准）
    · 表头：底色 #B7DEE8、微软雅黑 11pt 常规、垂直居中、自动换行、**四边**细线、行高 38
    · 正文：微软雅黑 11pt、垂直居中、自动换行；细线颜色 **纯黑 FF000000**（不是灰）
      - 非合并列 F/G/H/L：**每个步骤行都是完整四边框**（REF 实测 173/173 行 top&bottom 全 thin）
      - 合并列 A/B/C/D/E/I/J/K/M/N/O/P：块首行四边、块末行左/右/下、块中间行只有左/右
    · 列宽：A–L 显式指定；**M–P（截图1–4）同宽 12.65**
      ⚠️ 坑：REF 的 `<cols>` 里 M–P 是**一条** `<col min="13" max="16" width="12.65">`，
        openpyxl 的 `ws.column_dimensions` 只按首列 'M' 记账、查 N/O/P 会拿到默认值 ——
        别据此断定"截图2–4 没设宽"。判断列宽务必直接读 XML 的 `<cols>`。
    · sheetFormatPr：defaultColWidth=9、defaultRowHeight=16.8、无 baseColWidth（与 REF 一致）
    · 行高：**单行不设**（交给客户端自动），多行才显式设 16 × 折行数
      （REF 173 行里只有 86 行有 ht，取值 32/47/63/78 ≈ 16×2~5 行）
    · 合并区中间行的字体由 openpyxl 决定（写不进去），校验器对该处不做字体断言
    · 纵向合并：A B C D E I J K M N O P（F/G/H/L 不合并 —— 逐步骤取值）
    · 一行的意义 = 一个操作步骤；**每个步骤行都写自己的预期结果**
    · 「测试结果」列留空，由执行时回填；截图列留空
    · 用例编号形如 `INV_001`（模块前缀_三位序号）；优先级取值 P0/P1/P2/P3
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ------------------------------------------------------------------ 常量

HEADER_FILL_RGB = "FFB7DEE8"
FONT_NAME = "微软雅黑"
FONT_SIZE = 11
HEADER_ROW_HEIGHT = 38
LINE_HEIGHT = 16.0     # 单行内容占的行高（与存量文件 16 / 32 的规律一致）
MIN_ROW_HEIGHT = 16.0
MAX_ROW_HEIGHT = 240.0
TEXT_COLS = 4          # 只对前 4 列之外的超长文本做折行估算用（见 _row_height）


@dataclass(frozen=True)
class Column:
    title: str
    width: float | None  # None = 不显式设置，走默认列宽（与 REF 的 N/O/P 一致）
    halign: str          # 正文水平对齐
    key: str             # 在用例对象里的字段名（None 表示由 writer 单独处理）
    merged: bool         # 是否按用例纵向合并


# 顺序即 A→P，禁止随意调整
TEMPLATE: tuple[Column, ...] = (
    Column("用例编号",           12.0,     "center", "case_id",     True),
    Column("优先级",             8.49,     "center", "priority",    True),
    Column("功能模块",           23.71,    "left",   "module",      True),
    Column("用例标题",           45.83,    "left",   "title",       True),
    Column("前置条件（可为空）",   35.89,    "left",   "precondition", True),
    Column("操作步骤",           48.0,     "left",   "action",      False),
    Column("预期结果（可为空）",   52.0,     "left",   "expect",      False),
    Column("测试数据（可为空）",   25.31,    "left",   "data",        False),
    Column("测试结果",           13.46,    "center", "result",      True),
    Column("可自动化",           12.65,    "center", "auto",        True),
    Column("开发自测（可为空）",   23.56,    "center", "dev_self",    True),
    Column("备注（可为空）",      17.46,    "left",   "note",        False),
    Column("截图1",              12.65,    "center", "shot1",       True),
    Column("截图2",              12.65,    "center", "shot2",       True),
    Column("截图3",              12.65,    "center", "shot3",       True),
    Column("截图4",              12.65,    "center", "shot4",       True),
)

MERGE_COL_INDEXES: tuple[int, ...] = tuple(
    i + 1 for i, c in enumerate(TEMPLATE) if c.merged
)
HEADER_TITLES: tuple[str, ...] = tuple(c.title for c in TEMPLATE)
COLUMN_WIDTHS: tuple[float | None, ...] = tuple(c.width for c in TEMPLATE)

# 细线 = 纯黑；实测 REF 所有边框 rgb 均为 FF000000
_THIN = Side(style="thin", color="FF000000")
_BORDER_ALL = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BORDER_NO_TOP = Border(left=_THIN, right=_THIN, top=None, bottom=_THIN)
_BORDER_LR = Border(left=_THIN, right=_THIN, top=None, bottom=None)

DEFAULT_COL_WIDTH = 9.0
DEFAULT_ROW_HEIGHT = 16.8


def border_for(col_index: int, offset: int, n_steps: int) -> Border:
    """某个用例块内第 offset 行、第 col_index 列的边框（规则见模块 docstring）。"""
    if not TEMPLATE[col_index - 1].merged:
        return _BORDER_ALL          # F/G/H/L：逐步骤取值 → 每行都是完整网格
    if offset == 0:
        return _BORDER_ALL          # 块首行
    if offset == n_steps - 1:
        return _BORDER_NO_TOP       # 块末行：左/右/下
    return _BORDER_LR               # 块中间行：只有左/右


# ------------------------------------------------------------------ 数据模型

@dataclass
class Step:
    """一个操作步骤行：操作步骤 + 该步骤自己的预期结果（+ 可选测试数据）。"""

    action: str
    expect: str = ""
    data: str = ""


@dataclass
class Case:
    case_id: str
    priority: str
    module: str
    title: str
    steps: list[Step] = field(default_factory=list)
    precondition: str = ""
    auto: str = ""
    dev_self: str = ""
    note: str = ""
    # 非写入列：环境要求（用于在 catalog 里找项目），见 resolve.py
    requires: str = ""


@dataclass
class Suite:
    title: str
    sheet_name: str
    cases: list[Case] = field(default_factory=list)
    source_doc: str = ""


# ------------------------------------------------------------------ 写入

def _display_units(text: str) -> int:
    """按"列宽单位"估算文本宽度：CJK 记 2，其它记 1。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _row_height(cells: dict[int, str]) -> float | None:
    """估算某一行需要多高：逐列算折行数，取最大值。

    单行返回 None → **不写 ht**，交给 Excel/WPS 自动（REF 173 行里只有 86 行带 ht，
    且所有带 ht 的行都 ≥32 = 16×2 行）；多行才显式设高。
    """
    lines = 1
    for idx, col in enumerate(TEMPLATE, start=1):
        text = str(cells.get(idx) or "")
        if not text:
            continue
        usable = max((col.width or DEFAULT_COL_WIDTH) - 1.5, 4)
        n = 0
        for seg in text.split("\n"):
            n += max(1, math.ceil(_display_units(seg) / usable))
        lines = max(lines, n)
    if lines <= 1:
        return None
    return min(lines * LINE_HEIGHT, MAX_ROW_HEIGHT)


CELL_FONT = Font(name=FONT_NAME, size=FONT_SIZE)
HEADER_FONT = Font(name=FONT_NAME, size=FONT_SIZE)
HEADER_FILL = PatternFill(fill_type="solid", start_color=HEADER_FILL_RGB, end_color=HEADER_FILL_RGB)


def write_workbook(suite: Suite, out_path: str | Path, sheet_name: str | None = None) -> Path:
    """按 TEMPLATE 生成用例 Excel。返回落盘路径。"""
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_name or suite.sheet_name or "Sheet1")[:31]

    # 列宽（A–P 全部显式设置；M–P 同宽 12.65，与 REF 的 <col min="13" max="16"> 一致）
    for i, col in enumerate(TEMPLATE, start=1):
        if col.width is not None:
            ws.column_dimensions[get_column_letter(i)].width = col.width
    ws.sheet_format.defaultColWidth = DEFAULT_COL_WIDTH
    ws.sheet_format.defaultRowHeight = DEFAULT_ROW_HEIGHT
    ws.sheet_format.baseColWidth = None       # REF 的 sheetFormatPr 里没有这一项

    # 表头
    for i, col in enumerate(TEMPLATE, start=1):
        c = ws.cell(row=1, column=i, value=col.title)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.border = _BORDER_ALL
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = HEADER_ROW_HEIGHT

    row = 2
    for case in suite.cases:
        steps = case.steps or [Step("", "", "")]
        first, last = row, row + len(steps) - 1

        for offset, step in enumerate(steps):
            r = first + offset
            # 每行的取值：合并列只在首行写，非合并列逐行写
            values: dict[int, str] = {}
            for i, col in enumerate(TEMPLATE, start=1):
                if col.merged:
                    continue
                if col.key == "action":
                    values[i] = step.action
                elif col.key == "expect":
                    values[i] = step.expect
                elif col.key == "data":
                    values[i] = step.data
                elif col.key == "note":
                    values[i] = ""            # 备注是"用例级"，写进合并后的首行
            if offset == 0:
                case_level = {
                    "case_id": case.case_id,
                    "priority": case.priority,
                    "module": case.module,
                    "title": case.title,
                    "precondition": case.precondition,
                    "result": "",                      # 测试结果留空待回填
                    "auto": case.auto,
                    "dev_self": case.dev_self,
                    "note": case.note,
                    "shot1": "", "shot2": "", "shot3": "", "shot4": "",
                }
                for i, col in enumerate(TEMPLATE, start=1):
                    if col.merged:
                        values[i] = case_level.get(col.key, "") or ""

            for i, col in enumerate(TEMPLATE, start=1):
                c = ws.cell(row=r, column=i)
                v = values.get(i)
                if v not in (None, ""):
                    c.value = v
                c.font = CELL_FONT
                c.alignment = Alignment(horizontal=col.halign, vertical="center", wrap_text=True)
                c.border = border_for(i, offset, len(steps))

            h = _row_height(values)
            if h:                       # 单行不设高度（客户端自动），多行才显式设
                ws.row_dimensions[r].height = h

        if last > first:      # 纵向合并（单步骤用例无需合并）
            for i in MERGE_COL_INDEXES:
                letter = get_column_letter(i)
                ws.merge_cells(f"{letter}{first}:{letter}{last}")
        row = last + 1

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ------------------------------------------------------------------ 校验

def validate_workbook(path: str | Path, expect_case_ids: bool = True) -> list[str]:
    """逐格核对产物是否符合 TEMPLATE。返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    wb = load_workbook(path)
    ws = wb.active

    # 1) 列结构
    if ws.max_column != len(TEMPLATE):
        problems.append(f"列数不对：期望 {len(TEMPLATE)} 列，实际 {ws.max_column} 列")
    for i, col in enumerate(TEMPLATE, start=1):
        got = ws.cell(row=1, column=i).value
        letter = get_column_letter(i)
        if got != col.title:
            problems.append(f"表头第 {i} 列（{letter}）应为「{col.title}」，实际「{got}」")
        w = ws.column_dimensions[letter].width
        if col.width is None:
            if w is not None:
                problems.append(f"列宽 {letter} 不应显式设置（与 REF 一致走默认），实际 {w}")
        elif w is None or abs(float(w) - col.width) > 0.05:
            problems.append(f"列宽 {letter} 应为 {col.width}，实际 {w}")

    # 1b) sheetFormat 默认值
    if ws.sheet_format.defaultColWidth not in (None, DEFAULT_COL_WIDTH):
        problems.append(f"默认列宽应为 {DEFAULT_COL_WIDTH}，实际 {ws.sheet_format.defaultColWidth}")
    if ws.sheet_format.defaultRowHeight not in (None, DEFAULT_ROW_HEIGHT):
        problems.append(f"默认行高应为 {DEFAULT_ROW_HEIGHT}，实际 {ws.sheet_format.defaultRowHeight}")

    # 2) 表头样式
    for i in range(1, len(TEMPLATE) + 1):
        c = ws.cell(row=1, column=i)
        letter = get_column_letter(i)
        if (c.fill.start_color.rgb or "") not in (HEADER_FILL_RGB, "00" + HEADER_FILL_RGB[2:]):
            problems.append(f"表头 {letter}1 底色应为 {HEADER_FILL_RGB}，实际 {c.fill.start_color.rgb}")
        if c.font.name != FONT_NAME or c.font.sz != FONT_SIZE:
            problems.append(f"表头 {letter}1 字体应为 {FONT_NAME} {FONT_SIZE}，实际 {c.font.name} {c.font.sz}")
        if not c.alignment.wrap_text or c.alignment.vertical != "center":
            problems.append(f"表头 {letter}1 应对齐为「自动换行 + 垂直居中」")
        if None in (c.border.left.style, c.border.right.style,
                    c.border.top.style, c.border.bottom.style):
            problems.append(f"表头 {letter}1 应为四边细线，实际 {c.border.left.style}/"
                            f"{c.border.right.style}/{c.border.top.style}/{c.border.bottom.style}")
        elif (c.border.left.color.rgb or "") not in ("FF000000", "00000000"):
            problems.append(f"表头 {letter}1 边框应为纯黑 FF000000，实际 {c.border.left.color.rgb}")

    # 3) 正文样式 + 合并块
    merges = {str(m): m for m in ws.merged_cells.ranges}
    blocks = [m for m in ws.merged_cells.ranges if m.min_col == 1 and m.max_row > m.min_row]
    blocks.sort(key=lambda m: m.min_row)

    checked_rows = set()
    for m in blocks:
        for r in range(m.min_row, m.max_row + 1):
            checked_rows.add(r)
        for i in range(1, len(TEMPLATE) + 1):
            letter = get_column_letter(i)
            hit = any(str(mm) == f"{letter}{m.min_row}:{letter}{m.max_row}"
                      for mm in ws.merged_cells.ranges)
            if TEMPLATE[i - 1].merged and not hit:
                problems.append(f"用例块 {m.min_row}-{m.max_row} 的 {letter} 列应纵向合并，实际未合并")
            if not TEMPLATE[i - 1].merged and hit:
                problems.append(f"用例块 {m.min_row}-{m.max_row} 的 {letter} 列不应合并，实际合并了")
        # 步骤行的预期结果必须逐行非空
        for r in range(m.min_row, m.max_row + 1):
            if not ws.cell(row=r, column=6).value:
                problems.append(f"第 {r} 行「操作步骤」为空")
            if not ws.cell(row=r, column=7).value:
                problems.append(f"第 {r} 行「预期结果」为空（约定：每个步骤行都要写自己的预期结果）")

    # 4) 未合并的单步骤用例也要有步骤与预期
    for r in range(2, ws.max_row + 1):
        if r in checked_rows:
            continue
        if not ws.cell(row=r, column=1).value and not ws.cell(row=r, column=6).value:
            continue
        if not ws.cell(row=r, column=7).value:
            problems.append(f"第 {r} 行「预期结果」为空")

    # 5) 编号唯一性 / 优先级取值
    if expect_case_ids:
        seen: dict[str, int] = {}
        for r in range(2, ws.max_row + 1):
            cid = ws.cell(row=r, column=1).value
            if not cid:
                continue
            if cid in seen:
                problems.append(f"用例编号重复：{cid}（第 {seen[cid]} 行 与 第 {r} 行）")
            seen[str(cid)] = r
            pr = ws.cell(row=r, column=2).value
            if pr not in ("P0", "P1", "P2", "P3"):
                problems.append(f"第 {r} 行「优先级」应为 P0/P1/P2，实际「{pr}」")

    # 6) 正文字体 / 边框（逐列按规则校验，而不是只看"至少有左/右/下"）
    block_of: dict[int, tuple[int, int]] = {}
    for m in blocks:
        for r in range(m.min_row, m.max_row + 1):
            block_of[r] = (m.min_row, m.max_row)
    # 合并区内非左上角的格子：字体由 openpyxl 决定（写进去也会被丢），不做字体断言
    inner_cells: set[tuple[int, int]] = set()
    for m in ws.merged_cells.ranges:
        for r in range(m.min_row, m.max_row + 1):
            for c in range(m.min_col, m.max_col + 1):
                if (r, c) != (m.min_row, m.min_col):
                    inner_cells.add((r, c))

    for r in range(2, ws.max_row + 1):
        first, last = block_of.get(r, (r, r))
        for i in range(1, len(TEMPLATE) + 1):
            c = ws.cell(row=r, column=i)
            letter = get_column_letter(i)
            if (r, i) not in inner_cells and (c.font.name != FONT_NAME or c.font.sz != FONT_SIZE):
                problems.append(f"{letter}{r} 字体应为 {FONT_NAME} {FONT_SIZE}，"
                                f"实际 {c.font.name} {c.font.sz}")
                continue
            want = border_for(i, r - first, last - first + 1)
            exp = tuple(getattr(s, "style", None) for s in
                        (want.left, want.right, want.top, want.bottom))
            got = (c.border.left.style, c.border.right.style,
                   c.border.top.style, c.border.bottom.style)
            if got != exp:
                problems.append(f"{letter}{r} 边框应为 {exp}，实际 {got}")
            elif (c.border.left.color.rgb or "") not in ("FF000000", "00000000"):
                problems.append(f"{letter}{r} 边框应为纯黑 FF000000，实际 {c.border.left.color.rgb}")

    # 6b) 行高：单行不应显式设高（< 32 说明把单行也写死了）
    for r in range(2, ws.max_row + 1):
        h = ws.row_dimensions[r].height
        if h is not None and h < 2 * LINE_HEIGHT - 0.5:
            problems.append(f"第 {r} 行高 {h} < {2 * LINE_HEIGHT}：单行不应显式设高")
            break
    if ws.freeze_panes:
        problems.append(f"不应设置冻结窗格，实际 {ws.freeze_panes}")
    return problems


def describe_template() -> str:
    """人类可读的格式说明（打印给用户看 / 写进文档）。"""
    lines = [f"{'列':<4}{'列名':<20}{'宽':>7}  {'正文对齐':<8}{'纵向合并':<8}"]
    for i, col in enumerate(TEMPLATE, start=1):
        width = "默认" if col.width is None else f"{col.width:.2f}"
        lines.append(f"{get_column_letter(i):<4}{col.title:<20}{width:>7}  "
                     f"{col.halign:<8}{'是' if col.merged else '否':<8}")
    lines.append("")
    lines.append(f"表头：底色 {HEADER_FILL_RGB} / {FONT_NAME} {FONT_SIZE}pt / 垂直居中 / 自动换行 / 四边黑细线 / 行高 {HEADER_ROW_HEIGHT}")
    lines.append("正文：微软雅黑 11pt / 垂直居中 / 自动换行 / 黑细线")
    lines.append("      F/G/H/L（操作步骤·预期结果·测试数据·备注）逐步骤取值 → 每行完整四边框")
    lines.append("      其余列按用例纵向合并 → 块首行四边、块末行左/右/下、块中间行只有左/右")
    lines.append(f"行高：单行不设（走默认 {DEFAULT_ROW_HEIGHT}），多行设 {LINE_HEIGHT} × 折行数")
    lines.append("一行 = 一个操作步骤；每个步骤行都写自己的预期结果；测试结果列留空待回填")
    return "\n".join(lines)
