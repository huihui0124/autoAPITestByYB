# -*- coding: utf-8 -*-
"""
probe_prod_multiverify_order.py —— 在生产上真造一张「多次核销场次」的票，看它给几次。

背景：
    config.PROD.supports_multi_verify=False 是 2026-09-29 的结论（票据接口不带次数字段、
    核销记录恒空）。2026-09-30 复跑发现生产电子票**已带 totalVerifyCount / verifyRecordList**。
    本脚本进一步验证"次数真的能用几次"——不止看字段在不在，还把次数真核销掉。

它会做的事（**写操作**，仅在生产测试店 888）：
    ① C 端 0 元下单（默认 P01 / 127 / sku=1039「多次核销场次（3次）」的 0 元档）
    ② 读 B 端电子票的 totalVerifyCount
    ③ 连续核销直到被拒（或到 --max 次），打印每次的响应与剩余次数
    ④ 读最终订单状态
全程只花 0 元。

跑法：
    MIGU_ALLOW_PROD_WRITE=1 MIGU_B_ENV=prod MIGU_ENV=prod PYTHONPATH=. \\
        ./.venv/bin/python tools/probe/probe_prod_multiverify_order.py [pid] [skuId]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

PID = int(sys.argv[1]) if len(sys.argv) > 1 else 127
SKU = int(sys.argv[2]) if len(sys.argv) > 2 else 1039
MAX_VERIFY = 6

from migu_api.badmin import BAdminClient  # noqa: E402
from migu_api.client import MiguClient  # noqa: E402


def show_counts(tag: str, t: dict) -> None:
    if not isinstance(t, dict):
        print(f"   [{tag}] 票对象类型异常：{type(t)}")
        return
    keys = ("totalVerifyCount", "usedVerifyCount", "remainVerifyCount", "status", "expired")
    print(f"   [{tag}] " + " ".join(f"{k}={t.get(k)}" for k in keys))
    rec = t.get("verifyRecordList")
    if rec is not None:
        print(f"   [{tag}] verifyRecordList {len(rec)} 条")


c = MiguClient()
c.login()
print(f"C 端登录 OK；下单 {PID}/{SKU} 1 张 0 元")
order_id = c.create_order(PID, SKU, 1, 0, source="probe_prod_multiverify_order")
print(f"orderId={order_id}")

code = None
deadline = time.time() + 40
while time.time() < deadline:
    ticket = c.get_eticket(order_id)
    items = ticket if isinstance(ticket, list) else [ticket]
    items = [t for t in items if t and t.get("code")]
    if items:
        code = items[0]["code"]
        break
    time.sleep(2)
if not code:
    print("✗ 40s 未出票，终止")
    raise SystemExit(1)
print(f"券码={code}")

b = BAdminClient()
print(f"B 端 storeId={b.store_id}；等 B 端可见…")
for _ in range(20):
    if b.wait_order(order_id, timeout=5):
        break
    time.sleep(2)

before = b.get_eticket(order_id)
print("\n① 核销前：")
show_counts("before", before)
total = before.get("totalVerifyCount") if isinstance(before, dict) else None
print(f"   → totalVerifyCount = {total!r}（1 = 单次核销；>1 = 已发布多次核销）")

print("\n② 连续核销：")
for i in range(1, MAX_VERIFY + 1):
    rc, msg = b.verify_result(code)
    print(f"   第 {i} 次核销 → code={rc} message={msg!r}")
    time.sleep(1)
    t = b.get_eticket(order_id)
    show_counts(f"after-{i}", t)
    if rc != 200:
        print("   （被拒，停止）")
        break

print("\n③ 最终订单状态：")
o = b.get_order(order_id)
print("   " + json.dumps({k: (o or {}).get(k) for k in ("status", "statusName", "statusText")},
                         ensure_ascii=False))
print("\n④ 核销记录：")
rec = b.verify_records(code)
print(f"   {len(rec)} 条 → {json.dumps(rec, ensure_ascii=False)[:400]}")
