# -*- coding: utf-8 -*-
"""
probe_prod_multiverify.py —— 【只读】探针：生产到底发布「多次核销」没有？

背景：
    config.PROD.supports_multi_verify=False 是 2026-09-29 的观测结论：
      · 票据接口 /v1/shopTrade/order/{id}/eticket 不返回 totalVerifyCount 等字段
      · 核销记录接口 /v1/verification/voucher 恒返回 []
    2026-09-30 跑生产核销链路时，**核销记录接口返回了 1 条数据**，
    于是用例里那条"能力开关可能该更新了"的守卫断言红了。
    本脚本只读地核三件事，判断开关该不该改：
      ① /v1/selfProduct/events/list —— 场次有没有 verificationNum / maxVerifyCount
      ② /v1/selfProduct/tickets/list —— 票档有没有 maxVerifyCount
      ③ 最近一笔生产订单的电子票 —— 有没有 totalVerifyCount/usedVerifyCount/remainVerifyCount
        以及 /v1/verification/voucher 有没有记录
    全程 **只读**，不下单、不核销、不改配置。

跑法：
    MIGU_B_ENV=prod PYTHONPATH=. ./.venv/bin/python tools/probe/probe_prod_multiverify.py [productId]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ENV = os.environ.get("MIGU_B_ENV", "prod")

from migu_api.badmin import BAdminClient  # noqa: E402

PID = int(sys.argv[1]) if len(sys.argv) > 1 else 127

b = BAdminClient()
print(f"环境 MIGU_B_ENV={ENV}  storeId={b.store_id}  productId={PID}\n")

COUNT_KEYS = ("totalVerifyCount", "usedVerifyCount", "remainVerifyCount",
              "maxVerifyCount", "verificationNum", "verifyCount",
              "multiVerify", "supportMultiVerify")


def dig(obj, path="$") -> list[str]:
    """递归找出所有计数字段的出现位置。"""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in COUNT_KEYS:
                hits.append(f"{path}.{k} = {v!r}")
            hits += dig(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            hits += dig(v, f"{path}[{i}]")
    return hits


# ---------- ① 场次级 ----------
print("=" * 70)
print("① GET /v1/selfProduct/events/list  —— 场次级核销配置")
try:
    ev = b._request("GET", "/v1/selfProduct/events/list",
                    params={"storeId": b.store_id, "productId": PID,
                            "pageIndex": 1, "pageSize": 50})
    rows = ev.get("data") or []
    print(f"   共 {len(rows)} 个场次")
    for r in rows:
        hits = dig(r)
        print(f"   · eventId={r.get('eventId')} {r.get('eventName')!r} "
              f"start={r.get('startTime')} end={r.get('endTime')}")
        print(f"       计数字段: {hits or '（无）'}")
    if not rows:
        print("   （原始返回）", json.dumps(ev, ensure_ascii=False)[:400])
except Exception as e:  # noqa: BLE001
    print(f"   ✗ {type(e).__name__}: {str(e)[:200]}")

# ---------- ② 票档级 ----------
print("=" * 70)
print("② GET /v1/selfProduct/tickets/list —— 票档级核销配置")
try:
    tk = b._request("GET", "/v1/selfProduct/tickets/list",
                    params={"storeId": b.store_id, "productId": PID,
                            "pageIndex": 1, "pageSize": 100})
    rows = tk.get("data") or []
    print(f"   共 {len(rows)} 个票档")
    for r in rows:
        hits = dig(r)
        print(f"   · skuId={r.get('id') or r.get('skuId')} {r.get('priceName')!r} "
              f"price={r.get('salePrice')} 计数字段: {hits or '（无）'}")
except Exception as e:  # noqa: BLE001
    print(f"   ✗ {type(e).__name__}: {str(e)[:200]}")

# ---------- ③ 电子票 + 核销记录 ----------
print("=" * 70)
print("③ 最近订单的电子票字段 + 核销记录")
try:
    orders = b.list_orders(page_size=20)
    rows = orders.get("data") if isinstance(orders, dict) else orders
    rows = rows or []
    print(f"   最近 {len(rows)} 笔订单")
    picked = None
    for r in rows:
        oid = r.get("id") or r.get("orderId")
        name = r.get("eventName") or r.get("productName") or ""
        print(f"   · order={oid} status={r.get('status')} {r.get('statusName')} {name!r}")
        # 挑一笔"已完成(7)"或"待核销(11)"的电子票来看
        if picked is None and r.get("status") in (7, 11, 2):
            picked = oid
    if picked is None and rows:
        picked = rows[0].get("id") or rows[0].get("orderId")

    if not picked:
        print("   （没有可用订单，跳过）")
    else:
        et = b.get_eticket(picked)
        print(f"\n   订单 {picked} 的电子票顶层字段: {sorted(et.keys()) if isinstance(et, dict) else type(et)}")
        print(f"   电子票里的计数字段: {dig(et) or '（无）'}")
        code = None
        codes = et.get("codeList") if isinstance(et, dict) else None
        if isinstance(codes, list) and codes:
            code = codes[0].get("code") if isinstance(codes[0], dict) else codes[0]
        code = code or (et.get("code") if isinstance(et, dict) else None)
        if code:
            rec = b.verify_records(code)
            print(f"   券码 {code} 的核销记录: {len(rec)} 条 → "
                  f"{json.dumps(rec, ensure_ascii=False)[:300]}")
        else:
            print("   （该票没有券码，跳过核销记录查询）")
except Exception as e:  # noqa: BLE001
    print(f"   ✗ {type(e).__name__}: {str(e)[:200]}")

print("=" * 70)
print("结论看上面三处的『计数字段』：有 → supports_multi_verify 该置 True。")
