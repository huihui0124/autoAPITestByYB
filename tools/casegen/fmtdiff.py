# -*- coding: utf-8 -*-
"""
fmtdiff.py —— 用例 Excel 的**外观指纹比对**

与 template.validate_workbook 的分工
    validate_workbook = 生成器自检：假定文件是本工具生成的，逐格对照 TEMPLATE，
                         发现即报错（CI 闸门）。
    fmtdiff          = 存量文件体检：对**任意**历史用例 Excel 只比外观，
                         允许行列数不同、内容不同，只回答"看起来像不像同一套模版"。
                         用来给 user 手上几十份历史用例做批量体检、找格式分叉。

为什么必须直读 XML
    · openpyxl 的 `ws.column_dimensions` 对 `<col min="13" max="16">` 这种"跨列一条"
      只按首列记账，查 N/O/P 会拿到默认值 → 会误判成"未设宽"。（踩过）
    · `ws.row_dimensions[r].height` 为 None 表示"没写 ht"（客户端自动），
      与"写了 16"是两种不同外观，必须区分。

指纹归一化（避免假报警）
    · XML 属性顺序 / 自闭合空格 → 忽略
    · 列宽：把 `<col>` 的 min..max 展开成"逐列宽度序列"再比
    · 行高：只比**规则**（单行是否设高、多行是否 ≈16×行数），不比具体数值
    · 边框颜色 → 归一成 black / other
    · 字体颜色 → 忽略（theme=1 与不写 视觉效果相同）
"""
from __future__ import annotations

import collections
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from . import xlcompat  # noqa: F401  —— 打兼容补丁，必须在 load_workbook 之前 import

# ------------------------------------------------------------------ 指纹

_NORM_WIDTH = 0.5          # 列宽容差（REF 用 8.4903846… 这种长小数）
ROW_LINE = 16.0            # 多行行的基准行高


@dataclass
class Fingerprint:
    path: Path
    n_cols: int = 0
    titles: list[str] = field(default_factory=list)
    widths: list[float] = field(default_factory=list)      # 逐列（已展开 min..max）
    default_col_width: float | None = None
    default_row_height: float | None = None
    header: tuple = ()                                     # 表头样式
    header_height: float | None = None
    merged_cols: list[str] = field(default_factory=list)    # 纵向合并的列
    borders: dict = field(default_factory=dict)            # (列, 块内位置) → 边框元组
    row_height_rule: dict = field(default_factory=dict)    # {"单行设高": 数, "多行设高": 数, "多行不设高": 数}
    haligns: dict = field(default_factory=dict)            # 列 → 对齐集合（仅有值的行）
    freeze: str | None = None
    style_readable: bool = True                            # False = 只读模式（取不到样式）


def _sheet_xml(path: Path) -> str:
    z = zipfile.ZipFile(path)
    names = [n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    return z.read(sorted(names)[0]).decode("utf-8")


def _cols_widths(xml: str) -> tuple[list[float], int]:
    """把 <cols> 展开成逐列宽度（处理 min..max 跨列）。"""
    widths: dict[int, float] = {}
    max_col = 0
    for m in re.finditer(r"<col\b[^>]*/>", xml):
        tag = m.group(0)
        w = re.search(r'width="([\d.]+)"', tag)
        lo = re.search(r'min="(\d+)"', tag)
        hi = re.search(r'max="(\d+)"', tag)
        if not (w and lo):
            continue
        lo_i = int(lo.group(1))
        hi_i = int(hi.group(1)) if hi else lo_i
        for c in range(lo_i, hi_i + 1):
            widths[c] = float(w.group(1))
        max_col = max(max_col, hi_i)
    return [widths.get(i) for i in range(1, max_col + 1)], max_col


def _bside(side) -> str | None:
    return getattr(side, "style", None)


def _bcolor(side) -> str:
    rgb = getattr(getattr(side, "color", None), "rgb", None) or ""
    return "black" if rgb in ("FF000000", "00000000", "") else str(rgb)


def _block_positions(ws) -> dict[int, str]:
    """行号 → 该行在所属用例块里的位置（首/中/末）。"""
    pos: dict[int, str] = {}
    for m in ws.merged_cells.ranges:
        if m.min_col != 1 or m.max_row <= m.min_row:
            continue
        for r in range(m.min_row, m.max_row + 1):
            pos[r] = "首" if r == m.min_row else ("末" if r == m.max_row else "中")
    return pos


def fingerprint(path: str | Path) -> Fingerprint:
    path = Path(path)
    xml = _sheet_xml(path)
    try:
        wb = openpyxl.load_workbook(path)
    except TypeError:
        # 有些"含结果"文件带了 openpyxl 3.1.5 不认识的 dataValidation 字段
        # （如 id=）。只读模式能绕过，但读不到样式 → 只给出列结构。
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        titles = list(next(ws.iter_rows(values_only=True)))
        fp = Fingerprint(path=path, n_cols=len(titles),
                         titles=[t for t in titles], style_readable=False)
        widths, _ = _cols_widths(xml)
        fp.widths = [round(w, 2) if w is not None else None for w in widths]
        return fp
    ws = wb.active
    fp = Fingerprint(path=path)

    fp.n_cols = ws.max_column
    fp.titles = [ws.cell(1, i).value for i in range(1, ws.max_column + 1)]
    widths, _ = _cols_widths(xml)
    fp.widths = [round(w, 2) if w is not None else None for w in widths]
    fp.default_col_width = ws.sheet_format.defaultColWidth
    fp.default_row_height = ws.sheet_format.defaultRowHeight

    h = ws.cell(1, 1)
    fp.header = (h.font.name, h.font.sz, bool(h.font.b),
                 h.fill.fgColor.rgb if h.fill.fill_type else None,
                 h.alignment.horizontal, h.alignment.vertical, bool(h.alignment.wrap_text),
                 (_bside(h.border.left), _bside(h.border.right),
                  _bside(h.border.top), _bside(h.border.bottom), _bcolor(h.border.left)))
    fp.header_height = ws.row_dimensions[1].height
    fp.freeze = ws.freeze_panes

    fp.merged_cols = sorted({get_column_letter(m.min_col)
                            for m in ws.merged_cells.ranges if m.max_row > m.min_row})

    pos_of = _block_positions(ws)
    borders = collections.Counter()
    height_rule = collections.Counter()
    haligns: dict[str, set] = collections.defaultdict(set)
    for r in range(2, ws.max_row + 1):
        pos = pos_of.get(r, "首")          # 非合并块 = 单步骤用例，视为"首"
        hgt = ws.row_dimensions[r].height
        n_lines = 1
        for i in range(1, ws.max_column + 1):
            c = ws.cell(r, i)
            letter = get_column_letter(i)
            key = (letter, pos, (_bside(c.border.left), _bside(c.border.right),
                                 _bside(c.border.top), _bside(c.border.bottom),
                                 _bcolor(c.border.left)))
            borders[key] += 1
            if c.value not in (None, ""):
                haligns[letter].add((c.alignment.horizontal, c.alignment.vertical,
                                     bool(c.alignment.wrap_text)))
        height_rule["单行设高" if (hgt and hgt < 2 * ROW_LINE) else
                    ("多行设高" if hgt else "未设高")] += 1
        del n_lines
    fp.borders = dict(borders)
    fp.row_height_rule = dict(height_rule)
    fp.haligns = {k: set(v) for k, v in haligns.items()}
    return fp


# ------------------------------------------------------------------ 比对

def diff(ref: Fingerprint, got: Fingerprint) -> tuple[list[str], list[str]]:
    """返回 (问题, 提示)。问题 = 外观不一致；提示 = 非外观差异（如某列没填内容）。"""
    problems: list[str] = []
    info: list[str] = []

    if ref.titles != got.titles:
        problems.append(f"列结构不同：基准 {len(ref.titles)} 列，本次 {len(got.titles)} 列\n"
                        f"      基准 {ref.titles}\n      本次 {got.titles}")

    # 列宽：只比两边都有的列
    n = min(len(ref.widths), len(got.widths))
    bad = [(get_column_letter(i + 1), ref.widths[i], got.widths[i])
           for i in range(n)
           if (ref.widths[i] is None) != (got.widths[i] is None)
           or (ref.widths[i] is not None and got.widths[i] is not None
               and abs(ref.widths[i] - got.widths[i]) > _NORM_WIDTH)]
    if bad:
        problems.append("列宽不同：" + "；".join(
            f"{L} 基准 {a} / 本次 {b}" for L, a, b in bad))

    if ref.default_col_width != got.default_col_width:
        problems.append(f"默认列宽不同：基准 {ref.default_col_width} / 本次 {got.default_col_width}")
    if ref.default_row_height != got.default_row_height:
        problems.append(f"默认行高不同：基准 {ref.default_row_height} / 本次 {got.default_row_height}")

    if not got.style_readable:
        info.append("该文件只能读到列结构（openpyxl 读不了它的 dataValidation 字段，"
                    "已退回只读模式），样式相关项（表头/边框/对齐/行高）未比对")
        return problems, info

    if ref.header[:4] != got.header[:4]:
        problems.append(f"表头样式不同：基准 {ref.header[:4]} / 本次 {got.header[:4]}")
    if ref.header[4:] != got.header[4:]:
        problems.append(f"表头对齐/边框不同：基准 {ref.header[4:]} / 本次 {got.header[4:]}")
    if ref.header_height != got.header_height:
        problems.append(f"表头行高不同：基准 {ref.header_height} / 本次 {got.header_height}")

    if ref.merged_cols != got.merged_cols:
        problems.append(f"纵向合并列不同：基准 {ref.merged_cols} / 本次 {got.merged_cols}")
    if ref.freeze != got.freeze:
        problems.append(f"冻结窗格不同：基准 {ref.freeze} / 本次 {got.freeze}")

    # 边框：按 (列, 块内位置) 归一后比较
    # 注意：key 的第三段是 (left,right,top,bottom,color)，里面可能含 None，
    # 直接 sorted(dict) 会拿 None 跟 'thin' 比大小 → TypeError。所以按 repr 排序。
    miss = sorted((k for k in ref.borders if k not in got.borders), key=repr)
    extra = sorted((k for k in got.borders if k not in ref.borders), key=repr)
    if miss or extra:
        problems.append("边框模式不同：\n"
                        + (f"      基准有而本次缺：{miss[:6]}{'…' if len(miss) > 6 else ''}\n" if miss else "")
                        + (f"      本次有而基准无：{extra[:6]}{'…' if len(extra) > 6 else ''}" if extra else ""))

    # 行高规则：基准里出现过的"坏规则"，本次也不该有
    for rule, cnt in got.row_height_rule.items():
        if rule not in ref.row_height_rule:
            problems.append(f"行高规则多出「{rule}」（{cnt} 行）——基准里没有这种写法")

    # 对齐：两边都有值的列才比
    for L, a in ref.haligns.items():
        b = got.haligns.get(L)
        if not b:
            if a:
                info.append(f"{L} 列：基准有值（{sorted(a)}），本次为空（不算格式问题）")
            continue
        if a != b:
            problems.append(f"{L} 列对齐不同：基准 {sorted(a)} / 本次 {sorted(b)}")
    for L, b in got.haligns.items():
        if L not in ref.haligns and b:
            info.append(f"{L} 列：基准为空，本次有值（{sorted(b)}）")
    return problems, info


def compare(ref_path: str | Path, cmp_path: str | Path) -> tuple[Fingerprint, Fingerprint, list[str], list[str]]:
    a, b = fingerprint(ref_path), fingerprint(cmp_path)
    problems, info = diff(a, b)
    return a, b, problems, info
