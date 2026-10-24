# -*- coding: utf-8 -*-
"""
make_multi_order.py —— 造一笔「多份」订单并生成付款二维码

为什么需要它：
    「部分退」用例要求订单的某个商品项有 **>= 2 份**（只退其中若干份）。
    店铺里现成的多份订单要么属于**其他账号**（本项目的硬约束：只允许操作
    18366267528 / 15550042537 的订单，绝不能动别人的），要么已被退成
    "账实不符"的历史脏数据。所以最干净的做法是**自己下一笔多份订单**。

用法：
    COUNT=3 ./.venv/bin/python make_multi_order.py                  # 测试环境（微信）
    COUNT=3 MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 \
        ./.venv/bin/python make_multi_order.py                      # 生产（支付宝）

产出：
    artifacts/<env>_multi_<orderId>_qr.png    付款二维码（真人对它付款）
    artifacts/<env>_multi_<orderId>_form.html 支付宝收银台表单页（扫不动时用）

下一步：付款成功后用刚下的单号跑部分退用例
    MIGU_REFUND_PARTIAL_ORDER=<orderId> ./run.sh refund
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from migu_api import paypage, qr
from migu_api import catalog
from migu_api.client import MiguClient, status_text
from migu_api.config import env, prod_write_blocked, prod_write_skip_reason
from migu_api.fsutil import ensure_dir

ARTIFACTS = Path(__file__).parent / "artifacts"
COUNT = max(1, int(os.environ.get("COUNT", "3")))
# 渠道默认取环境配置（测试 WX / 生产 AP），可用 MIGU_PAY_CHANNEL=AP 强制覆盖
CHANNEL = os.environ.get("MIGU_PAY_CHANNEL", env.pay_bank_code).upper()


def main() -> None:
    if prod_write_blocked():
        raise SystemExit(f"已拦截：{prod_write_skip_reason()}")

    # 目标从「测试专用商品清单」按用途取（usage=paid），不写死商品 ID
    try:
        target = catalog.one(usage="paid")
    except catalog.CatalogError as exc:
        raise SystemExit(f"无法从测试专用商品清单取到下单目标：\n{exc}")
    product_id, sku_id, unit = target.product_id, target.sku_id, target.price
    total = round(float(unit) * COUNT, 2)

    client = MiguClient()
    client.login()
    print(f"环境 {env.name} | 店铺 {env.store_id} | 账号 {client.phone}")
    print(f"商品 {product_id}/{sku_id} | 单价 {unit} × {COUNT} 份 = {total} 元")

    order_id = client.create_order(product_id, sku_id, COUNT, total,
                                    source="make_multi_order")
    detail = client.get_order_detail(order_id)
    print(f"订单 {order_id} 状态 {status_text(detail.get('status'))}")
    assert detail.get("status") == 1, f"下单后应为待付款，实际 {detail.get('status')}"

    pay_detail = client.get_pay_detail(order_id)
    print(f"倒计时 {int(pay_detail.get('leftTime') or 0) // 1000}s | 渠道 {CHANNEL}")

    pay = client.create_payment(order_id, bank_code=CHANNEL, pay_amount=total)
    params = pay.get("paymentParams") or ""
    assert params, f"未返回支付参数：{pay}"
    print("支付参数形态:", paypage.describe(params))

    ensure_dir(ARTIFACTS)
    form_html = None
    if paypage.is_link(params):
        pay_url = paypage.strip_cdata(params)
    else:
        form_html = paypage.save_form_html(
            params, ARTIFACTS / f"{env.name}_multi_{order_id}_form.html")
        pay_url = paypage.to_pay_url(params)

    png = qr.save_png(pay_url, ARTIFACTS / f"{env.name}_multi_{order_id}_qr.png")
    meta = {"env": env.name, "storeId": env.store_id, "orderId": order_id,
            "count": COUNT, "unitPrice": unit, "totalAmount": total,
            "channel": CHANNEL, "qrPng": str(png),
            "formHtml": str(form_html) if form_html else None}
    (ARTIFACTS / f"{env.name}_multi_order.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 64)
    print(f"请扫码支付 {total} 元（{COUNT} 份）")
    print(f"二维码：{png}")
    if form_html:
        print(f"表单页：{form_html}")
    print("=" * 64)
    print(f"付完后跑：MIGU_REFUND_PARTIAL_ORDER={order_id} ./run.sh refund")


if __name__ == "__main__":
    main()
