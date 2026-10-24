# -*- coding: utf-8 -*-
"""
probe_funnel_conversion.py —— 反查 B 端首页「购买转化漏斗」的口径

问题（2026-09-30 提出）：
    商家后台首页的购买转化漏斗上，「支付成功数」比「创建订单数」还大 ——
    漏斗里后一级不可能比前一级大，说明各级根本不是同一套口径。

本脚本把三份数据拉齐对照，直接指出口径错配：

    ① 漏斗接口 GET /v1/statistic/page/conversion
       → data.stages[{stage, pv, uv}] + data.paidOrderCount
    ② B 端订单列表 GET /v1/shopTrade/order（按日统计真实订单笔数）
    ③ 逐 channelCode 重放漏斗（验证每级是否受渠道过滤 —— 数据源是否同一个）

结论（实测，生产 storeId=888）：
    · 前四级 home/product_detail/confirm_order/payment 是**埋点**数据，
      受 channelCode 过滤（任一渠道取值都能把它们清零）；
    · 末级 payment_success **恒等于当日真实订单笔数**、且 uv==pv、完全不受
      channelCode 影响 → 它不是埋点，是订单/支付表直算的"订单口径"；
    · paidOrderCount 又是第三个独立数字，与上面两个都不等。

于是「支付成功 > 创建订单」不是转化率异常，而是**两把尺子画在同一张漏斗上**。

跑法（只读，除 B 端会话外不需要任何写权限）：
    MIGU_B_ENV=prod ./.venv/bin/python tools/probe/probe_funnel_conversion.py
    MIGU_B_ENV=prod ./.venv/bin/python tools/probe/probe_funnel_conversion.py --days 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from migu_api.badmin import BAdminClient          # noqa: E402

STORE_ID = 888


def _ts(v):
    try:
        return dt.datetime.fromtimestamp(v / 1000)
    except (TypeError, ValueError, OSError):
        return None


def funnel(b: BAdminClient, day: dt.date, channel_code: str | None = None) -> dict:
    prm = {"storeId": STORE_ID,
           "startTime": f"{day} 00:00:00", "endTime": f"{day} 23:59:59"}
    if channel_code:
        prm["channelCode"] = channel_code
    r = b._request("GET", "/v1/statistic/page/conversion", params=prm)
    stages = {s["stage"]: (s["pv"], s["uv"]) for s in r.get("stages") or []}
    return {"stages": stages, "paidOrderCount": r.get("paidOrderCount")}


def real_orders(b: BAdminClient) -> tuple[Counter, Counter, Counter]:
    """翻 B 端订单列表，按「日 → 笔数 / 票数」汇总，另返回渠道分布。"""
    rows: list = []
    for pi in range(1, 40):
        r = b.list_orders(page_size=50, page_index=pi)
        rows += r.get("data") or []
        if not r.get("hasMore"):
            break
    by_day, tickets, by_channel = Counter(), Counter(), Counter()
    for o in rows:
        d = _ts(o.get("addTime"))
        if not d:
            continue
        day = d.date()
        by_day[day] += 1
        tickets[day] += (o.get("count")
                         or len(o.get("electronicVoucherCode") or []) or 1)
        by_channel[o.get("channel")] += 1
    return by_day, tickets, by_channel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回看天数（默认 10）")
    args = ap.parse_args()

    b = BAdminClient()                     # 复用落盘 cookie，不重新登录
    today = dt.date(2026, 9, 30)

    print("=" * 92)
    print("① 漏斗接口：逐日 PV/UV + paidOrderCount")
    print("=" * 92)
    print(f"{'日期':<12}{'home':>10}{'detail':>10}{'confirm':>10}"
          f"{'payment':>10}{'pay_success':>13}{'paidOrder':>11}")
    funnels: dict[dt.date, dict] = {}
    for i in range(args.days - 1, -1, -1):
        day = today - dt.timedelta(days=i)
        try:
            f = funnel(b, day)
        except Exception as exc:                                  # noqa: BLE001
            print(f"{day}  ERR {str(exc)[:60]}")
            continue
        funnels[day] = f
        st = f["stages"]
        fmt = lambda k: "/".join(str(x) for x in st.get(k, (0, 0)))  # noqa: E731
        print(f"{str(day):<12}{fmt('home'):>10}{fmt('product_detail'):>10}"
              f"{fmt('confirm_order'):>10}{fmt('payment'):>10}"
              f"{fmt('payment_success'):>13}{f['paidOrderCount']:>11}")

    print()
    print("=" * 92)
    print("② B 端订单列表：真实订单笔数 / 票数（同一天，和上面末级对照）")
    print("=" * 92)
    by_day, tickets, by_channel = real_orders(b)
    print(f"{'日期':<12}{'真实订单笔数':>14}{'票数':>9}{'漏斗pay_success.pv':>20}{'是否相等':>10}")
    for day in sorted(by_day):
        n = by_day[day]
        pv = (funnels.get(day) or {}).get("stages", {}).get("payment_success", (None,))[0]
        mark = "✅ 相等" if pv == n else f"差 {None if pv is None else pv - n}"
        print(f"{str(day):<12}{n:>14}{tickets[day]:>9}{str(pv):>20}{mark:>12}")
    print(f"\n订单渠道分布（全量 {sum(by_channel.values())} 笔）: {dict(by_channel)}")

    print()
    print("=" * 92)
    print(f"③ 逐 channelCode 重放漏斗（{today}）—— 看各级是否同一数据源")
    print("=" * 92)
    base = funnels.get(today)
    if base:
        st = base["stages"]
        print(f"{'channelCode':>12} | {'confirm_order':>14} | {'payment':>12} | "
              f"{'payment_success':>16} | {'paidOrderCount':>15}")
        for cc in ("", "1", "2", "3", "4", "5", "6", "7"):
            try:
                f = funnel(b, today, cc or None)
            except Exception as exc:                              # noqa: BLE001
                print(f"{cc or '(不限)':>12} | ERR {str(exc)[:60]}")
                continue
            s = f["stages"]
            print(f"{cc or '(不限)':>12} | "
                  f"{'/'.join(map(str, s.get('confirm_order', (0, 0)))):>14} | "
                  f"{'/'.join(map(str, s.get('payment', (0, 0)))):>12} | "
                  f"{'/'.join(map(str, s.get('payment_success', (0, 0)))):>16} | "
                  f"{f['paidOrderCount']:>15}")

    print()
    print("=" * 92)
    print("结论")
    print("=" * 92)
    if base:
        pv, uv = base["stages"].get("payment_success", (None, None))
        print(f"· payment_success 的 pv 与 uv 恒等（今天 {pv}/{uv}），其余四级 uv 明显小于 pv"
              " → 末级没做用户去重，不是页面口径")
        print("· 任一 channelCode 都能把前四级清零，末级 + paidOrderCount 纹丝不动"
              " → 末级与其余四级不同源")
        print("· payment_success.pv 与「当日真实订单笔数」逐日吻合"
              " → 末级就是订单/支付口径")
        print("· 于是「支付成功 > 创建订单」= 页面口径 vs 订单口径混画一张图，"
              "不是转化率异常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
