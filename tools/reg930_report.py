# -*- coding: utf-8 -*-
"""
reg930_report.py —— 把 930 回归的执行结果回填成《回归用例（含结果）.xlsx》+ Markdown 报告

输入：
    · 源用例   <930目录>/回归用例.xlsx                  （权威用例表，只读）
    · 结论台账 reports/reg930/verdicts.json            （用例级口径：通过/部分通过/需人工）
    · junit    reports/reg930/junit.xml                （兜失败原因与跳过原因）

输出：
    · <930目录>/回归用例（含结果）.xlsx                  （回填「测试结果」「结果备注」两列）
    · reports/reg930/930回归测试报告.md                 （结论、逐条判定、发现、遗留数据）

为什么要两个来源：junit 只有 passed/failed/skipped 三态，而 930 用例需要
「通过 / 部分通过 / 需人工」——后者是**用例口径**，只有用例自己知道，所以落台账；
junit 负责提供机器判定的失败原因，避免台账漏记时报告"无备注"。

用法：
    ./.venv/bin/python tools/reg930_report.py
    ./.venv/bin/python tools/reg930_report.py --source <回归用例.xlsx> --out-xlsx <输出.xlsx>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font

ROOT = Path(__file__).resolve().parent.parent
# ⚠️ 以 `./.venv/bin/python tools/reg930_report.py` 方式执行时，sys.path[0] 是 tools/
#    而不是仓库根，`import migu_api` 会失败 —— 必须自己把根目录挂上去。
#    （曾经因为这里静默 except 吞掉 ImportError，订单终态一栏全是"—"。）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_SOURCE = Path("/Users/yubing/Documents/haomai/haomai-migu/930测试用例V1.2.0/回归用例.xlsx")
DEFAULT_LEDGER = ROOT / "reports" / "reg930" / "verdicts.json"
DEFAULT_JUNIT = ROOT / "reports" / "reg930" / "junit.xml"

# 结果 → 字体色（测试结果列着色，便于一眼扫过）
RESULT_COLOR = {
    "通过": "FF1F7A3D",        # 深绿
    "部分通过": "FFB26A00",    # 橙
    "未通过": "FFC00000",      # 红
    "未自动化": "FF7F7F7F",    # 灰
    "未执行": "FF7F7F7F",
}

# 探索期在 888 店留下的既有订单（不属于本次回归，但要登记，避免被当成"新产生的脏数据"）
PREEXISTING = [
    ("61045508", "非选座-仅快递 0 元单，已收货(status=6)", "接口勘察期（09-30 之前）"),
    ("61045332", "非选座-仅自取 0 元单，已取(status=6)", "接口勘察期（09-30 之前）"),
    ("61045382", "选座+实名 0 元单，待使用(status=11)", "接口勘察期（09-30 之前）"),
]

# 补充用例：**不由 930 用例表派生**，是按产品口径补的自动化加固用例。
# 追加在结果表末尾（源用例表只读、不改动），保证「结果表 = 源表 + 补充」可追溯。
SUPPLEMENT_SPEC = [
    ("TC-019", "退款三种方式：B端条件退 / B端常规退 / C端条件退（补充用例）"),
    ("TC-020", "C端条件退全链路：0.01元真实支付 → C端自助退款 → 订单已退款（补充用例）"),
]

# 三条退款通路的口径表（写进报告，说明"用例里那句『申请退款』到底该走哪条"）
REFUND_PATHS_MD = """
### 退款三种方式（2026-09-30 生产 888 实测口径）

| 方式 | 发起入口 | 退什么 | 是否需审批 | 关键接口 |
|---|---|---|---|---|
| ① B端条件退 | **单笔订单的「订单详情」** | 可选 仅退票 / 仅退款 / 退票+退款 | 需（退款审批） | `GET /v1/shopTrade/refund/refundable/{id}` → `POST /v1/shopTrade/refund/partial` → `POST /v1/shopTrade/refund/approval` |
| ② B端常规退 | **「退款中心」** | **只能全量退票+退款** | 需（退款审批菜单） | `GET /v1/shopTrade/order/refundCenterSearch` → `POST /v2/shopTrade/purchaseOrder/batchRefund` → `POST /v2/shopTrade/purchaseOrder/batchRefund/approve` |
| ③ C端条件退 | **C 端订单详情** | 全额自助退 | **无需审批** | `GET /v1/order/refund/{id}/pre` → `POST /v1/order/refund/{id}/submit`（TC-020 真金闭环：0.01 元单提交即受理 → 订单 5/已退款） |

**③ C端条件退的四个并列硬约束**（缺一不可，括号内为实测原文）：

1. **场次必须配置了条件退** → 否则 `canShowRefund=false`，refundTip
   「本项目不支持退款」，**C 端连退款入口都不出现**。
   （实测：条件退场次 3241 的单 `canShowRefund=true`；非条件退场次 3235 的单 `false`。）
2. **必须在条件退时间范围内** → 否则即使入口展示，也不能发起自助条件退。
3. **订单不能是终态**（已核销/已收货/已完成）→ 否则 `canRefund=false`，
   refundTip「订单已经完成啦～如需退款，联系客服为您处理哦！」。
   注意：终态**不会**让入口消失（`canShowRefund` 仍为 true），只是不能点。
4. **退款金额不能为 0** → 0 元单不得用于 C 端申请退款。
   ⚠️ 这一条在 `refund_pre` 里只能看到 refundAmount=0；release_core 不对 0 元单调用
   submit，真正的 C 端提交成功路径由 TC-020 使用非 0 元真实支付单覆盖。

> 因此 888 店的 **0 元票档夹具结构上跑不了 C 端条件退**；而 TC-005~008 的步骤又
> 先在商家端核销/收货把订单推到终态、再「申请退款」，所以这几条用例的 C 端分支
> **必然不可达** —— 套件按用例步骤里并列的「商家端订单详情」分支走 ① B端条件退。
> 三条通路本身由补充用例 **TC-019** 逐条覆盖。

**③ 的「真退成功」由补充用例 TC-020 补齐**（TC-019 只验到约束、没验到成功）：

TC-019 用 0 元夹具只能证明入口与终态等部分约束，**证明不了「C 端条件退真的能退成功」**——
因为 0 元单不能用于 C 端申请退款，而套件原本没有任何一笔"真付过钱"的单。
TC-020 用清单中**条件退场次的非 0 元电子票票档**（"付费且配了条件退"的交集）
真金跑一遍：

    下单 0.01 元 → 支付宝真付 0.01 元 → 订单 11/待使用
      → C端 pre：时间窗内 canShowRefund=true & canRefund=true & refundAmount=0.01
      → C端 submit **无需审批，直接受理**
      → 回读：订单落 5/已退款（C 端与 B 端两侧一致）

> 这条用例受**真实支付闸门**控制（`migu_api/fundgate.py`），默认 `off` 时整条跳过、
> 不产生任何资金流水；跑法：
>
> ```bash
> MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 \\
>     ./.venv/bin/pytest -m reg930 -k TC020 -s --real-pay=on
> ```
>
> （会打印二维码等人扫 0.01 元）。失败补跑用 `MIGU_PAY_ORDER=<已付订单号>`
> 复用那笔已付款单，**不要重复支付**。
"""


# ---------------------------------------------------------------- 读取输入

def load_ledger(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {_norm(r["case"]): r for r in rows}


def load_junit(paths) -> dict[str, dict]:
    """→ {testcase_name: {"outcome": passed/failed/skipped, "message": ...}}

    支持传多个 junit 文件：整轮跑（junit.xml）与「补跑失败用例」（junit_fix.xml）
    各留一份，合并后才是完整的一轮结果 —— 避免为了凑一份整齐的 junit 而重跑整轮、
    又在生产测试店多造一批订单。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    out: dict[str, dict] = {}
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        tree = ET.parse(path)
        for tc in tree.iter("testcase"):
            name = tc.get("name") or ""
            outcome, message = "passed", ""
            for tag in ("failure", "error", "skipped"):
                node = tc.find(tag)
                if node is not None:
                    outcome = {"failure": "failed", "error": "failed",
                               "skipped": "skipped"}[tag]
                    message = (node.get("message") or "").strip() or (node.text or "").strip()
                    break
            out[name] = {"outcome": outcome, "message": message}
    return out


def _norm(key: str) -> str:
    """用例键归一化：`TC-018a` / `TC018a` / `tc018A` 视为同一个键。

    用例文件里人手写 `TC-018a`，函数名派生出 `TC018a`，源表里又是 `TC-018`
    —— 三处写法不一致会静默地"对不上"（表现为结果对、备注空）。
    统一去连字符 + 转小写，从根上消掉这类不一致。
    """
    return re.sub(r"[^0-9a-z]", "", (key or "").lower())


def case_key_of(testname: str) -> str | None:
    """用例函数名 → 台账 key。TC-018 拆两行，靠函数名里的 seat/nonseat 区分。"""
    m = re.match(r"test_(TC\d{3})", testname)
    if not m:
        return None
    tc = m.group(1)
    if tc == "TC018":
        if "nonseat" in testname:
            return "TC018b"
        if "seat" in testname:
            return "TC018a"
        return None
    return f"{tc[:3]}-{tc[3:]}"


def row_case_key(sheet_no: str, title: str) -> str:
    """表里的行 → 台账 key（TC-018 用标题里的「非选座」区分）。"""
    if sheet_no.upper() == "TC-018":
        return "TC018b" if "非选座" in (title or "") else "TC018a"
    return sheet_no


# ---------------------------------------------------------------- 判定合并

def merge(case_key: str, ledger: dict, junit_by_case: dict) -> tuple[str, str]:
    """台账口径优先；junit 兜失败/跳过原因。返回 (测试结果, 备注)。"""
    entry = ledger.get(_norm(case_key)) or {}
    j = junit_by_case.get(_norm(case_key)) or {}
    outcome, jmsg = j.get("outcome"), (j.get("message") or "").strip()

    if outcome == "failed":
        result = "未通过"
        note = jmsg or (entry.get("note") or "")
    elif outcome == "skipped":
        result = entry.get("result") or "未自动化"
        note = entry.get("note") or jmsg
    elif outcome == "passed":
        result = entry.get("result") or "通过"
        note = entry.get("note") or ""
    else:                                     # 没收集到（被整组 skip 掉，比如生产写闸门没开）
        result = entry.get("result") or "未执行"
        note = entry.get("note") or "本轮未执行（用例未收集到，检查闸门/环境）"

    orders = entry.get("orders") or []
    if orders:
        note = f"{note}（订单：{'、'.join(str(o) for o in orders)}）" if note else \
               f"订单：{'、'.join(str(o) for o in orders)}"
    if outcome == "skipped" and jmsg and jmsg not in note:
        note = f"{note} ｜跳过原因：{jmsg}" if note else f"跳过原因：{jmsg}"
    return result, note


# ---------------------------------------------------------------- 输出

def write_xlsx(source: Path, out: Path, ledger: dict, junit_by_case: dict,
               env_note: str) -> tuple[list[dict], dict]:
    wb = openpyxl.load_workbook(source)
    ws = wb.active
    sheets: list[dict] = []

    for r in range(2, ws.max_row + 1):
        no = ws.cell(r, 1).value
        title = ws.cell(r, 5).value or ""
        if not no:
            continue
        key = row_case_key(str(no).strip(), str(title))
        result, note = merge(key, ledger, junit_by_case)
        ws.cell(r, 9, result)
        ws.cell(r, 10, note)
        color = RESULT_COLOR.get(result)
        if color:
            for col in (9, 10):
                cell = ws.cell(r, col)
                cell.font = Font(name=cell.font.name or "微软雅黑",
                                 size=cell.font.size or 11, color=color)
        ws.cell(r, 9).alignment = Alignment(horizontal="center", vertical="center",
                                            wrap_text=True)
        ws.cell(r, 10).alignment = Alignment(horizontal="left", vertical="center",
                                             wrap_text=True)
        sheets.append({"row": r, "case": str(no).strip(), "key": key, "title": str(title),
                       "result": result, "note": note})

    # 补充用例：源表里没有，追加在末尾（源表本身只读，不改结构）
    for no, title in SUPPLEMENT_SPEC:
        key = no
        result, note = merge(key, ledger, junit_by_case)
        r = ws.max_row + 2
        ws.cell(r, 1, no)
        ws.cell(r, 5, title)
        ws.cell(r, 9, result)
        ws.cell(r, 10, note)
        color = RESULT_COLOR.get(result)
        if color:
            for col in (9, 10):
                cell = ws.cell(r, col)
                cell.font = Font(name=cell.font.name or "微软雅黑",
                                 size=cell.font.size or 11, color=color)
        ws.cell(r, 9).alignment = Alignment(horizontal="center", vertical="center",
                                            wrap_text=True)
        ws.cell(r, 10).alignment = Alignment(horizontal="left", vertical="center",
                                             wrap_text=True)
        sheets.append({"row": r, "case": no, "key": key, "title": title,
                       "result": result, "note": note})

    # 末尾追加一行执行环境说明（不破坏原表结构，只是多一行可见的元信息）
    meta_row = ws.max_row + 2
    ws.cell(meta_row, 1, "执行环境")
    ws.cell(meta_row, 2, env_note).font = Font(name="微软雅黑", size=10, color="FF7F7F7F")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    stats: dict[str, int] = {}
    for s in sheets:
        stats[s["result"]] = stats.get(s["result"], 0) + 1
    return sheets, stats


def probe_order_states(order_ids: list[str]) -> dict[str, dict]:
    """现场回读 B 端订单终态（拿不到会话就返回空 dict，报告降级为只列单号）。

    这一步是「报告说的和店里的一致」的保证：订单号是执行时记的，
    终态是**生成报告这一刻**从店里读回来的，避免手写状态漂移。
    """
    if not order_ids:
        return {}
    try:                                     # 延迟导入：没有 B 端环境也能生成报告
        from migu_api.badmin import BAdminClient
    except Exception as exc:                 # noqa: BLE001
        print(f"  （无法导入 B 端客户端，订单终态降级：{type(exc).__name__}: {exc}）",
              file=sys.stderr)
        return {}
    try:
        b = BAdminClient()
        out: dict[str, dict] = {}
        for oid in order_ids:
            row = b.get_order(oid)
            if row:
                out[str(oid)] = row
        return out
    except Exception as exc:                 # noqa: BLE001
        print(f"  （B 端回读订单终态失败，报告降级：{type(exc).__name__}）", file=sys.stderr)
        return {}


def write_markdown(sheets: list[dict], stats: dict, env_note: str, out: Path,
                   extra: dict) -> None:
    total = len(sheets)
    lines: list[str] = []
    lines.append("# 咪咕赛事票务系统一期 V1.2.0 —— 930 回归用例执行报告")
    lines.append("")
    n_src = len([s for s in sheets if all(s["case"] != no for no, _ in SUPPLEMENT_SPEC)])
    n_sup = total - n_src
    lines.append(f"- **用例来源**：《930测试用例V1.2.0/回归用例.xlsx》「核心流程回归」"
                 f"（{n_src} 行 / 15 条用例）"
                 + (f"，外加**补充用例 {n_sup} 条**（见下表末尾，按产品退款口径加固）"
                    if n_sup else ""))
    lines.append(f"- **判定行数**：{total} 行")
    lines.append(f"- **执行环境**：{env_note}")
    lines.append(f"- **执行时间**：{extra.get('at') or datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append("- **自动化套件**：`test_930_regression.py`（分组 `reg930`）")
    lines.append("- **执行命令**：`MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 "
                 "./.venv/bin/pytest -m reg930 -q -rs`")
    lines.append("")
    lines.append("## 一、结论总览")
    lines.append("")
    order = ["通过", "部分通过", "未通过", "未自动化", "未执行"]
    lines.append("| 结果 | 条数 | 占比 |")
    lines.append("|---|---:|---:|")
    for k in order:
        if stats.get(k):
            lines.append(f"| {k} | {stats[k]} | {stats[k] / total:.0%} |")
    lines.append(f"| **合计** | **{total}** | 100% |")
    lines.append("")
    lines.append("## 二、逐条判定")
    lines.append("")
    lines.append("| 行 | 用例编号 | 用例标题 | 测试结果 | 结果备注 |")
    lines.append("|---:|---|---|---|---|")
    for s in sheets:
        title = s["title"].replace("|", "/")
        note = s["note"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {s['row']} | {s['case']} | {title} | {s['result']} | {note} |")
    lines.append("")
    for heading, body in (extra.get("sections") or []):
        lines.append(heading)
        lines.append("")
        if body:
            lines.append(body)
            lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="930 回归结果回填 + 报告生成")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument("--junit", type=Path, nargs="+", default=None,
                    help="junit xml，可传多个（默认扫 reports/reg930/junit*.xml）")
    ap.add_argument("--out-xlsx", type=Path, default=None)
    ap.add_argument("--out-md", type=Path, default=None)
    args = ap.parse_args()

    src = args.source
    base = src.parent if src.parent != Path(".") else ROOT
    out_xlsx = args.out_xlsx or (base / "回归用例（含结果）.xlsx")
    out_md = args.out_md or (ROOT / "reports" / "reg930" / "930回归测试报告.md")

    if not src.exists():
        print(f"✗ 找不到源用例：{src}", file=sys.stderr)
        return 2

    ledger = load_ledger(args.ledger)
    junit_paths = args.junit or sorted(DEFAULT_JUNIT.parent.glob("junit*.xml"))
    junit_names = load_junit(junit_paths)
    junit_by_case = {}
    for name, info in junit_names.items():
        key = case_key_of(name)
        if key:
            junit_by_case[_norm(key)] = info

    orders: list[str] = []
    for entry in ledger.values():
        for o in entry.get("orders") or []:
            if str(o) not in orders:
                orders.append(str(o))

    env_note = ("生产 storeId=888「咪咕生产测试店」（ticket.miguvideo.com）；"
                "C端账户/C端接口 + B端商家后台/核销台；"
                "0元票档夹具为主（TC-020 另用 0.01 元票档真实支付），真实下单/核销/退款")

    # ---- 订单终态核对（能连上 B 端就现场回读，连不上就只列单号）----
    states = probe_order_states(orders)
    rows_md = []
    for o in orders:
        st = states.get(o)
        rows_md.append(
            f"| `{o}` | {st.get('statusName', '—') if st else '—'} "
            f"| {st.get('deliveryTypeName', '—') if st else '—'} "
            f"| {st.get('ticketCategorySpecification', '—') if st else '—'} |"
        )

    dirty = ["## 三、本次执行产生的数据（生产 888 店）", "",
             "本套件是**真实写操作**（下单/核销/发货/退款），执行会在 888 测试店留下订单。"
             "本次执行产生的订单及其终态（现场回读 B 端）：", "",
             "| 订单号 | 终态 | 交付方式 | 票档 |", "|---|---|---|---|"]
    dirty += rows_md if rows_md else ["| — | — | — | — |"]
    dirty += ["",
              "> 888 店是生产环境的测试专用店，**storeId=1 真实业务店全程未被触碰**；"
              "除补充用例 TC-020 的 **0.01 元真实支付**（C 端条件退要求退款金额≠0，"
              "只能真付）外，其余全部为 0 元票档，无其他真实资金流动。"
              "店内存量测试订单（本轮之前各阶段产生）不在本表内，未做任何清理。",
              "",
              "### 探索期遗留（非本次回归产生，登记备查）", ""]
    dirty += [f"- `{o}` —— {desc}（{when}）" for o, desc, when in PREEXISTING]

    findings = ["## 四、本轮发现（环境限制与行为观察，均非缺陷）", "",
                "1. **退款有三条通路，别混为一谈**：①B端条件退、②B端常规退、③C端条件退，"
                "入口、可控范围、是否审批各不相同（详见下方「退款三种方式」专节）。"
                "其中 ③ 有四个**并列**的硬约束：场次配了条件退（决定入口有无）、"
                "在条件退时间范围内、订单非终态（决定能否点）、退款金额≠0（决定 submit 能否过）。"
                "888 店的 0 元夹具 + 用例自身「先核销再申请退款」的步骤，使 ③ 在本组用例里"
                "**结构上不可达** —— 这是环境与用例设计的必然结果，不是缺陷。"
                "此前报告只写了「终态关闭」，口径不完整，已按实测补全。"
                "三条通路的**闭环能力**也已补齐：①B端条件退 / ②B端常规退 由 TC-019 实测闭环，"
                "③C端条件退由 TC-020 以 0.01 元真实支付闭环（提交后无需审批即受理 → 5/已退款）。",
                "2. **`/v1/shopTrade/order/realNameSearch` 在 888 恒返回空**："
                "按订单号/手机号/姓名/身份证前缀检索均为 0 条，而订单本身确实带实名人"
                "（如 `61045382` 携 `于*/371***748`）。TC-010 的「按身份证搜单」环节"
                "因此无法自动验证，需在 PDA 上人工确认；建议 B 端产品确认该接口的入参语义"
                "（是缺必要筛选条件，还是检索索引未覆盖 888）。",
                "3. **生产无短信能力可观测**：`sms_log_url=None`（app-7001.log 是 beta 独有），"
                "TC-015/016 的短信文案与到达只能人工核对；套件已为 TC-015 备好真实出票单。",
                "4. **实名认证走二要素核验**：生产 `/v1/user/realNameInfo/save` 会把姓名+证件号"
                "送外部征信，编造号码一律 500 被拒。自动化只能覆盖字段级负向；"
                "正向认证需用真实身份人工执行（本轮复用账号内既有实名人跑通了选座实名下单）。",
                "5. **生产场次级核销次数配置未发布**：所有票 `totalVerifyCount` 恒为 1，"
                "「多次核销」业务能力在生产不可覆盖（套件已把 total==1 作为口径固化断言）。",
                "6. **退出登录不可自动化**：真调 `POST /v1/logout` 会作废生产 `hmcer`，"
                "而通行证续期通道已失效、无法自愈（一次真退 = 后续全废）。套件改为无登录态契约校验。",
                "7. **开售提醒订阅是项目级能力差异**：`POST /v1/prefabricate/product/message` 对"
                "P08/P09/P10/P11 四个选座项目报 `500 该商品不在预售状态`，P01~P07 与 P12 正常。"
                "套件改为探测式选品并把不可订阅的项目如实记录，避免换机器跑时重复踩坑。",
                "8. **`purchaseOrderId` ≠ `orderId`（发货/收货/退款共同的隐蔽坑）**：B 端发货、收货"
                "与**常规退**要的都是**采购单号**（订单列表行的 `purchaseOrderId` 字段，如 722/848），"
                "传 orderId 只会得到 `code=500 系统错误`（后端不回字段名），"
                "极易被误判成「接口有 bug」。已在 `badmin.deliver/receive` 与 "
                "`refund.regular_refund` 里做成自动回查。",
                "9. **C 端取消订单必须带 `reason`**：`POST /v1/order/{id}/cancel` 的 `reason` 是"
                "必填 query 参数，缺了报 `400 Required String parameter 'reason' is not present`"
                "（已修正 `client.cancel_order`）。",
                "10. **C 端退款提交有约 10 秒的「落库窗口」**：下单后 `refund_pre` 立刻正常，"
                "但 `POST /v1/order/refund/{id}/submit` 会先连续返回 "
                "`400 无效订单，请联系客服~` 约 10 秒，之后才进入真正的业务校验。"
                "把「无效订单」当业务结论会把环境时序问题误报成「0 元单拦截缺失」"
                "（套件已加就绪重试 `_c_refund_submit_ready`）。",
                "11. **退款中心列表有索引延迟且默认按 `addTime` 倒序**："
                "`GET /v1/shopTrade/order/refundCenterSearch` 新单要 3~10s 才可检索；"
                "找刚下的单应传 `keyword=<订单号>` 做精确单条检索并重试，别翻页（实测可用）。",
                "12. **两条审批的字段形态完全不同**：条件退审批 "
                "`POST /v1/shopTrade/refund/approval` 用 `approvalStatus=2`（2 是**同意**）；"
                "常规退审批 `POST /v2/shopTrade/purchaseOrder/batchRefund/approve` 用 "
                "`approved: true`（布尔）。照抄必错。",
                "13. **C 端条件退的「真退成功」必须用带金额且处于条件退时间窗内的单，"
                "0 元夹具结构上覆盖不到**：硬约束之一就是「退款金额不能为 0」，0 元单不发起 C 端申请。"
                "所以套件把它做成受**真实支付闸门**控制的可选用例 "
                "**TC-020**（默认跳过、不花钱），用清单中条件退场次的非 0 元电子票票档真付真退 —— "
                "这才是「C 端条件退能不能退成功、退完订单落什么状态」的唯一证据。",
                "14. **生产收银台只有支付宝、且返回 HTML 表单**：888 店 "
                "`cashierPaymentMethods.wechat=false`，`create_payment(bankCode=AP)` 返回 "
                "`alipay.trade.wap.pay` 的**表单串**（不是微信那样的 https 链接），"
                "要落成 .html 或转成等价 GET 链接再出二维码；订单收银台倒计时实测 "
                "`leftTime≈899s`（支付宝侧 `timeout_express=29m`），**未在窗口内付款订单会被自动关闭**，"
                "补跑只能重开一单（或改用 `MIGU_PAY_ORDER` 续跑已付单）。"]

    extra = {
        "at": None,
        "sections": [("\n".join(dirty), ""), ("\n".join(findings), ""),
                     (REFUND_PATHS_MD, "")],
    }

    try:
        sheets, stats = write_xlsx(src, out_xlsx, ledger, junit_by_case, env_note)
    except Exception as exc:                              # noqa: BLE001
        print(f"✗ 回填 xlsx 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    write_markdown(sheets, stats, env_note, out_md, extra)

    print(f"✓ 含结果用例表：{out_xlsx}")
    print(f"✓ 执行报告    ：{out_md}")
    print("  结果分布：" + "，".join(f"{k} {v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
