# -*- coding: utf-8 -*-
"""
pay_scan.py —— 真实支付闭环：下单 → 引导付款 → 等订单变已支付 → 取电子票

为什么要脚本 + 人工配合：
    支付是真的（微信走 tenpay prepay_id / 支付宝走 openapi.alipay.com），
    必须真人付款 0.01 元，再靠网关回调把订单置为已支付。这一步没法纯自动化，
    所以做成"脚本创建订单并盯结果、人只负责付款"的半自动闭环。

渠道差异（实测 2026-09-29）：
    WX  微信：返回链接 → 出二维码 PNG（微信扫）
    AP  支付宝：返回 HTML 表单 → 落成 .html 打开浏览器（或转成等价 GET 链接出二维码）

————————————————————————————————————————————————————————————
两段式用法（推荐：先把码给你，再在后台盯结果，不用占着终端）
    # ① 出码（秒级），成功后把 artifacts/<env>_pay_qr_<orderId>.png 给用户扫
    ./.venv/bin/python pay_scan.py --prepare
    # ② 拿着上一步的 orderId 在后台等付款 + 取票
    ./.venv/bin/python pay_scan.py --order 12345678 --timeout 900

一段式用法（原行为，前台一直等到付完）
    ./.venv/bin/python pay_scan.py                     # 走环境默认渠道，等 240s
    ./.venv/bin/python pay_scan.py --channel WX --timeout 300
    ./.venv/bin/python pay_scan.py --no-open           # 支付宝表单不自动开浏览器

环境：
    MIGU_ENV=test （默认） / MIGU_ENV=prod
    商品、渠道、下单店铺全部从 migu_api/config.py 的 profile 读，**不写死**——
    否则会在生产拿测试商品 ID 下单（无效请求，还可能误触真实库存）。
    生产是写操作，另需 MIGU_ALLOW_PROD_WRITE=1 显式放行（和 run.sh 同一道闸门）。

产出（artifacts/ 下，文件名带环境前缀，测试/生产互不覆盖）：
    <env>_pay_qr_<orderId>.png      二维码（微信/支付宝都能扫）
    <env>_pay_form_<orderId>.html   支付宝收银台表单页（自动提交）
    <env>_last_pay.json             本轮完整结果（订单号/状态/电子票）
    last_pay.json                   （兼容旧路径，内容同上）

注意：订单创建后不付款会被自动关闭（收银台 leftTime 倒计时，实测约 2 分钟），
      脚本会打印倒计时；真关闭了会明确报"已关闭"，重跑一次出码即可。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from migu_api import catalog, paypage, qr                          # noqa: E402
from migu_api.fsutil import ensure_dir                              # noqa: E402
from migu_api.client import (MiguClient, status_text, STATUS_CLOSED,  # noqa: E402
                             PAID_STATUS)
from migu_api.config import env, prod_write_blocked, prod_write_skip_reason  # noqa: E402

ARTIFACTS = Path(__file__).parent / "artifacts"

# 下单目标从「测试专用商品清单」按用途取（usage=paid）；取不到就拒绝，不乱下单
try:
    _PAY_TARGET = catalog.one(usage="paid")
    PRODUCT_ID, SKU_ID, TOTAL_AMOUNT = (
        _PAY_TARGET.product_id, _PAY_TARGET.sku_id, _PAY_TARGET.price)
except catalog.CatalogError:
    _PAY_TARGET = None
    PRODUCT_ID, SKU_ID, TOTAL_AMOUNT = (None, None, "0.01")


def _dump(tag: str, result: dict) -> Path:
    """结果落盘：带环境前缀 + 兼容旧路径 last_pay.json。"""
    ensure_dir(ARTIFACTS)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    out = ARTIFACTS / f"{env.name}_{tag}.json"
    out.write_text(text, encoding="utf-8")
    if tag == "last_pay":
        (ARTIFACTS / "last_pay.json").write_text(text, encoding="utf-8")
    return out


def _gate() -> int | None:
    """生产写操作闸门 + 商品登记检查，不通过返回退出码。"""
    if PRODUCT_ID is None:
        print("「测试专用商品清单」(catalog.json) 里没有 usage=paid 的可下单目标。\n"
              "    先在 B 端建好 0.01 元电子票商品，再用 ./run.sh catalog add 登记。",
              file=sys.stderr)
        return 2
    if prod_write_blocked():
        print(f"✗ 已拦截：{prod_write_skip_reason()}", file=sys.stderr)
        return 3
    return None


def prepare(client: MiguClient, channel: str, no_open: bool) -> tuple[int | None, dict]:
    """① 下单 + 创建支付记录 + 落二维码，返回 (orderId, 上下文)。"""
    order_id = client.create_order(PRODUCT_ID, SKU_ID, 1, TOTAL_AMOUNT,
                                   source="pay_scan.prepare")
    detail = client.get_order_detail(order_id)
    print(f"[1/4] 订单已创建 orderId={order_id} "
          f"状态={status_text(detail.get('status'))}")

    pay_detail = client.get_pay_detail(order_id)
    left_ms = int(pay_detail.get("leftTime") or 0)
    print(f"[2/4] 收银台：应付 {pay_detail.get('payAmount')} 元，"
          f"渠道 {[c.get('type') for c in pay_detail.get('channels', [])]}，"
          f"倒计时 {left_ms // 1000}s")

    pay = client.create_payment(order_id, bank_code=channel, pay_amount=TOTAL_AMOUNT)
    params = pay.get("paymentParams") or ""
    if not params:
        print(f"[!] 未拿到支付参数：{pay}", file=sys.stderr)
        return None, {"orderId": order_id, "error": "no paymentParams", "pay": pay}
    print(f"[3/4] 支付参数形态：{paypage.describe(params)}")

    ctx = {"orderId": order_id, "channel": channel, "env": env.name,
           "storeId": env.store_id, "productId": PRODUCT_ID, "skuId": SKU_ID,
           "amount": TOTAL_AMOUNT, "orderStatus": detail.get("status"),
           "leftTimeSec": left_ms // 1000, "paid": False}

    if paypage.is_link(params):
        pay_url = paypage.strip_cdata(params)
        png = qr.save_png(pay_url, ARTIFACTS / f"{env.name}_pay_qr_{order_id}.png")
        ctx.update(payUrl=pay_url, qrPng=str(png))
        print(f"      微信支付链接二维码：{png}")
    else:
        html = paypage.save_form_html(
            params, ARTIFACTS / f"{env.name}_pay_form_{order_id}.html")
        pay_url = paypage.to_pay_url(params)
        png = qr.save_png(pay_url, ARTIFACTS / f"{env.name}_pay_qr_{order_id}.png")
        ctx.update(payUrl=pay_url, formHtml=str(html), qrPng=str(png))
        print(f"      支付宝收银台表单页：{html}")
        print(f"      等价支付链接二维码：{png}")
        if not no_open:
            subprocess.run(["open", str(html)], check=False)
            print("      已尝试用默认浏览器打开表单页（会自动跳转支付宝）")

    print(f"\n{'=' * 64}\n请支付 {TOTAL_AMOUNT} 元（{env.name} 环境 / 店铺 {env.store_id}）\n"
          f"二维码：{png}\n链接：{pay_url[:200]}\n{'=' * 64}")
    print(f"[4/4] 出码完成，orderId={order_id}"
          f"（订单倒计时 {left_ms // 1000}s，尽快付）")
    return order_id, ctx


def wait_and_collect(client: MiguClient, order_id: int, ctx: dict,
                     timeout: int, interval: int) -> dict:
    """② 等人工付款 → 校验订单状态 → 取电子票。"""
    print(f"[1/3] 等待支付结果（最长 {timeout}s，订单 {order_id}）…")
    t0 = time.time()
    # 生产没有 test/forceSync 这类调试后门（实测会挂死 30s），不做补单，纯等回调
    paid = client.wait_for_paid(order_id, timeout=timeout, interval=interval,
                                verbose=True)
    cost = int(time.time() - t0)
    detail = client.get_order_detail(order_id)
    status = detail.get("status")
    print(f"      结果：paid={paid} status={status_text(status)} 耗时={cost}s")
    ctx.update(orderStatus=status, paid=paid, paidCostSec=cost,
               paymentPrice=detail.get("paymentPrice"),
               waitMaxSec=timeout)

    if not paid and status == STATUS_CLOSED:
        ctx["hint"] = "订单已超时关闭（未在倒计时内付款）——重跑 --prepare 出码即可"
        print(f"      ⚠️ {ctx['hint']}")
        return ctx

    if status in PAID_STATUS:
        # 订单状态自身就是最强证据，即便 paySuccess 探针没命中也算支付成功
        ctx["paid"] = True
        ctx["paySuccessApi"] = paid
        ctx["orderStatusText"] = status_text(status)

    print(f"[2/3] {'✓ 已支付' if ctx['paid'] else '✗ 未支付'}，"
          f"金额={detail.get('paymentPrice')}")

    if ctx["paid"]:
        try:
            eticket = client.get_eticket(order_id)
            ctx["eticket"] = eticket
            print("[3/3] ✓ 电子票已出票：")
            print(json.dumps(eticket, ensure_ascii=False, indent=1)[:1500])
        except Exception as e:                     # noqa: BLE001 —— 出票失败不该吞掉支付结论
            print(f"[3/3] 取电子票失败：{e}")
            ctx["eticketError"] = str(e)
    else:
        print("[3/3] 未支付成功，跳过电子票")
    return ctx


def main() -> int:
    ap = argparse.ArgumentParser(
        description="真实支付闭环（半自动：脚本下单盯结果，人扫码付款）")
    ap.add_argument("--channel", choices=["WX", "AP"], default=None,
                    help=f"支付渠道，默认取环境配置（当前 {env.name}={env.pay_bank_code}）")
    ap.add_argument("--timeout", type=int, default=240, help="等待支付的秒数")
    ap.add_argument("--interval", type=int, default=3, help="轮询间隔秒")
    ap.add_argument("--no-open", action="store_true", help="支付宝表单不自动开浏览器")
    ap.add_argument("--prepare", action="store_true",
                    help="只出码不做等待（配合 --order 用于后台盯结果）")
    ap.add_argument("--order", type=int, default=None,
                    help="对已有订单等付款（跳过下单/出码）")
    args = ap.parse_args()

    channel = (args.channel or env.pay_bank_code).upper()

    if args.order:
        # 续跑模式：只等结果，不新建订单
        client = MiguClient()
        client.login()
        detail = client.get_order_detail(args.order)
        ctx = {"orderId": args.order, "channel": channel, "env": env.name,
               "orderStatus": detail.get("status"), "paid": False,
               "resumed": True}
        print(f"▶ 续跑订单 {args.order}（{env.name}），当前状态="
              f"{status_text(detail.get('status'))}")
        ctx = wait_and_collect(client, args.order, ctx, args.timeout, args.interval)
        print(f"\n结果已写入 {_dump('last_pay', ctx)}")
        return 0 if ctx.get("paid") else 1

    code = _gate()
    if code is not None:
        return code

    print(f"▶ 环境 MIGU_ENV={env.name}｜店铺 {env.store_id}｜商品 {PRODUCT_ID}/{SKU_ID}"
          f"｜金额 {TOTAL_AMOUNT}｜渠道 {channel}")
    client = MiguClient()
    client.login()
    print(f"  登录账号 {client.phone}")

    order_id, ctx = prepare(client, channel, args.no_open)
    if order_id is None:
        _dump("last_pay", ctx)
        return 2

    if args.prepare:
        out = _dump(f"pending_{order_id}", ctx)
        print(f"待付信息已写入 {out}")
        return 0

    ctx = wait_and_collect(client, order_id, ctx, args.timeout, args.interval)
    print(f"\n结果已写入 {_dump('last_pay', ctx)}")
    return 0 if ctx.get("paid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
