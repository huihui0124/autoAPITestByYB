# -*- coding: utf-8 -*-
"""
client.py —— 业务接口客户端（用例只跟这个类打交道）

设计要点：
    - 所有请求自动带登录态（hmcer Cookie），401 自动刷新重试一次
    - 所有响应统一过 _check：code != 200 直接抛 MiguApiError，
      用例里就不用到处写 assert resp["code"] == 200
    - 业务方法（get_member_info 等）按链路逐渐补充，每个方法一行文档
      对应 Apifox 里的一个接口

用例里的典型用法：
    client = MiguClient()
    info = client.get_member_info()
    assert info["phone"] == "15550042537"
"""
import json
import time

import requests

from . import catalog
from . import passport
from .config import env

# ---------------- 订单状态字典 ----------------
# 实测确认（2026-09-29，真实支付 + B 端核销全跑通后回填）：
#   1  = 待付款（orderDescTitle.title="待付款"，desc="订单自动取消倒计时"）
#   11 = 待使用（已付款！orderDescTitle.title="待使用"，desc="观演时凭电子票二维码入场"）
#   7  = 已完成（B 端核销完所有次数后；title="已完成"，desc="交易成功"）
#   4  = 已关闭（超时未付自动关闭）
#   5  = 已退款（2026-09-29 退款闭环实测回填：C 端与 B 端都变 5）
#   8  = 待自取（实物/自取类商品的履约状态）
#   14 = 售后中（实物/自取类商品退款后进入此态；电子票类直接到 5）
# 坑：已支付不是常见的 2/3/5，而是 11；完成也不是 3/5 而是 7。
#     (巧合：C 端和 B 端在 7 上都表示"已完成"，11 两边也都表示"待核销/待使用")
STATUS_UNPAID = 1
STATUS_PAID = 11
STATUS_FINISHED = 7
STATUS_CLOSED = 4
STATUS_REFUNDED = 5
STATUS_AFTER_SALE = 14
PAID_STATUS = {11, 7}            # 只要落在这里就说明钱到了（7 是核销完之后的终态）
# 退款成功后订单可能落的状态：电子票→5，实物/自取类→14（都是"退款已受理"）
REFUNDED_STATUS = {5, 14}
# 15 = 部分退（2026-09-29 实测：只退部分份数后订单落到这个状态，
#      与 5(整单已退款) 区别开——它表示"还留着没退的部分，可以继续退"）
STATUS_PARTIAL_REFUNDED = 15
ORDER_STATUS_TEXT = {
    1: "待付款",
    4: "已关闭",
    5: "已退款",
    7: "已完成",
    8: "待自取",
    11: "待使用(已付款)",
    14: "售后中",
    15: "部分退",
}


def status_text(status) -> str:
    """把订单 status 码翻译成人话，日志里好读。"""
    return f"{status}({ORDER_STATUS_TEXT.get(status, '未知')})"


class MiguApiError(AssertionError):
    """业务码非 200 时抛出。继承 AssertionError 是为了让 pytest 把它当断言失败。"""

    def __init__(self, method: str, path: str, status: int, body):
        self.status, self.body = status, body
        preview = json.dumps(body, ensure_ascii=False)[:300]
        super().__init__(f"{method} {path} -> HTTP {status}: {preview}")


class MiguClient:
    """咪咕票务（好麦通）C 端接口客户端。"""

    RETRYABLE_METHODS = {"GET", "HEAD"}

    def __init__(self, phone: str | None = None):
        self.session = passport.build_session()
        # 默认用 config 里登记的第一个测试账号
        self.phone = phone or next(iter(env.accounts), None)
        self._hmcer: str | None = None

    # ---------------- 登录态管理 ----------------

    def login(self) -> None:
        """确保登录态可用（有缓存用缓存，失效自动刷新）。"""
        self._hmcer = passport.ensure_hmcer()
        self._apply_hmcer()

    def _apply_hmcer(self) -> None:
        """把 hmcer 以 Cookie 形式挂到 session 上，之后每个请求自动携带。

        ⚠️ 域必须是**接口域名**（env.api_host），不是通行证域名。
        测试环境两者同域所以看不出差别；生产上通行证是 passport.migu.cn、
        接口是 ticket.miguvideo.com，挂错域这个 Cookie 根本不会被发出去。
        """
        self.session.cookies.set("hmcer", self._hmcer, domain=env.api_host)

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送 C 端请求；读请求遇到超时做短重试，写请求不自动重放。"""
        request_kwargs = dict(kwargs)
        request_kwargs.setdefault("timeout", env.http_timeout)

        tries = 3 if method.upper() in self.RETRYABLE_METHODS else 1
        last: requests.exceptions.RequestException | None = None
        for i in range(tries):
            try:
                return self.session.request(method, url, **request_kwargs)
            except (requests.exceptions.ReadTimeout,
                    requests.exceptions.ConnectTimeout) as exc:
                last = exc
                if i == tries - 1:
                    raise
                time.sleep(1.5 * (i + 1))
        raise last  # pragma: no cover - loop always returns or raises

    def _request(self, method: str, path: str, *, auth: bool = True,
                 retry: bool = True, **kwargs) -> dict:
        """统一请求入口：拼完整 URL、发请求、校验业务码、401 自愈。

        auth=False 表示免登录接口（如 /v1/shop），不携带也不准备登录态。
        """
        if auth and self._hmcer is None:
            self.login()
        elif auth and not self.session.cookies.get("hmcer"):
            self._apply_hmcer()

        url = f"{env.api_base}{path}"
        resp = self._send(method, url, **kwargs)

        # 401/403 多半是 hmcer 过期：强制刷新登录态后重试一次
        if auth and resp.status_code in (401, 403) and retry:
            self._hmcer = passport.force_refresh_hmcer()
            self._apply_hmcer()
            return self._request(method, path, auth=auth, retry=False, **kwargs)

        try:
            body = resp.json()
        except ValueError:
            raise MiguApiError(method, path, resp.status_code, {"raw": resp.text[:300]})

        if body.get("code") != 200:
            raise MiguApiError(method, path, resp.status_code, body)
        return body.get("data", body)

    # ---------------- 业务接口（对应 Apifox 文档） ----------------

    def get_member_info(self) -> dict:
        """GET /v1/customer/getInfoV2 —— 会员信息（手机号/积分/订单数）。

        登录态的核心校验点：返回的 phone 应等于测试账号手机号。
        """
        return self._request("GET", f"/v1/customer/getInfoV2?storeId={env.store_id}")

    def get_shop_info(self) -> dict:
        """GET /v1/shop —— 店铺信息（免登录）。顺带当连通性探针用。"""
        return self._request("GET", f"/v1/shop?storeId={env.store_id}", auth=False)

    # ---------------- 下单 -> 支付链路 ----------------

    def get_product_detail(self, product_id: int) -> dict:
        """GET /v2/product/{id} —— 商品详情（含 deliveryTypes/realNameLimit）。"""
        return self._request(
            "GET", f"/v2/product/{product_id}?storeId={env.store_id}", auth=False
        )

    def get_sku_list(self, product_id: int) -> list:
        """GET /v2/product/{id}/sku —— SKU 列表（价格/库存/是否选座）。"""
        data = self._request(
            "GET", f"/v2/product/{product_id}/sku?storeId={env.store_id}", auth=False
        )
        return data.get("skuList", []) if isinstance(data, dict) else data

    def create_order(self, product_id: int, sku_id: int, count: int = 1,
                     total_amount: str = "0.01",
                     contact_name: str = "自动化测试",
                     contact_mobile: str | None = None,
                     source: str = "") -> int:
        """POST /v2/order/makeOrder —— 创建订单，返回 orderId（int）。

        ★ 白名单闸门：下单前会比对「测试专用商品清单」（catalog.json），
          (productId, skuId) 不在清单登记范围就直接抛 CatalogViolation。
          C 端下单只允许落在测试专用项目上，避免误耗别的商品库存、
          产生说不清来源的测试数据。清单里没有就先登记：
              ./run.sh catalog list          # 看清单
              ./run.sh catalog add …         # 追加项目
          （排查时可临时 MIGU_CATALOG_ENFORCE=0 绕过。）

        body 结构严格对齐前端 confirm 页（逆向 chunk 26 确认）：
        - delivery 必传！缺了后端会报误导性的 "Source must not be null"
          （那是 Spring BeanUtils.copyProperties(null) 的经典报错）
        - 电子票 delivery.type=4；totalAmount 是字符串（前端 toFixed(2)）
        - channel=2 表示 WECHAT_H5（WECHAT_MINI=1 / DOUYIN=3 / XHS=6 / XY=7）
        """
        catalog.guard_order(product_id, sku_id, source=source)
        body = {
            "hmProductId": product_id,
            "skuId": sku_id,
            "count": count,
            "postage": 0,
            "totalAmount": total_amount,
            "channel": 2,
            "delivery": {
                "type": 4,
                "name": contact_name,
                "mobileNo": contact_mobile or self.phone,
            },
            "promotionAmount": 0,
        }
        data = self._request(
            "POST", f"/v2/order/makeOrder?storeId={env.store_id}", json=body
        )
        return data["orderId"]

    def get_order_detail(self, order_id) -> dict:
        """GET /v1/order/{id} —— 订单详情。status: 1=待支付 4=已关闭。"""
        return self._request(
            "GET", f"/v1/order/{order_id}?storeId={env.store_id}"
        )

    def create_payment(self, order_id, bank_code: str | None = None,
                       pay_amount: str = "0.01") -> dict:
        """POST /v1/miguPay/pay —— 创建支付记录（真实收银台入口）。

        返回 {"paymentParams": ..., "success": true, "needVerify": false}。
        subjectType=4 表示订单（逆向前端确认）。

        bank_code 决定渠道，**默认取环境的默认渠道**（env.pay_bank_code）：
            测试 = "WX"（微信/支付宝都开着）
            生产 = "AP"（888 店收银台 wechat=false，只有支付宝）
        "WX" = 微信 H5，paymentParams 是 https://wx.tenpay.com/... 链接，扫码即可付
        "AP" = 支付宝，paymentParams 可能是 HTML form（支付宝 wap 支付老协议），
               需要落到本地 html 再用浏览器打开
        """
        bank_code = bank_code or env.pay_bank_code
        return self._request(
            "POST", f"/v1/miguPay/pay?storeId={env.store_id}",
            json={
                "subjectId": str(order_id),
                "subjectType": "4",
                "paymentAmount": pay_amount,
                "payRedirectUrl": (
                    f"{env.base_url}/saishi/pages/order/payment"
                    f"?orderId={order_id}&storeId={env.store_id}&isHost=true&pending=true"
                ),
                "payConfigType": "",
                "bankCode": bank_code,
            },
        )

    def test_pay(self, order_id, pay_amount: str = "0.01",
                 pass_id: str = "16769985", migu_product_id: str = "952") -> dict:
        """POST /v1/miguPay/test/pay —— 测试后门：向 Ruby 网关发起支付请求。

        passId=咪咕用户中心ID（实测 tpid 可用）；miguProductId=952 来自文档示例。
        返回 transactionId / wapPayParam / requestXml / responseXml。

        ⚠️ 重要限制（文档原文："不涉及业务逻辑，仅验证协议对接"）：
        这个接口不创建支付记录，所以拿它返回的 wapPayParam 去付款，
        钱会扣但**订单不会变成已支付**。它只适合验证网关协议是否通。
        真实支付请用 create_payment()。
        """
        return self._request(
            "POST", f"/v1/miguPay/test/pay?storeId={env.store_id}",
            json={
                "orderId": str(order_id),
                "payAmount": pay_amount,
                "bankCode": "WX",
                "passId": pass_id,
                "miguProductId": migu_product_id,
            },
        )

    # ---------------- 真实支付闭环（人工扫码 + 结果轮询） ----------------

    def get_pay_detail(self, order_id) -> dict:
        """GET /v1/unifiedPay/payDetail —— 收银台详情（金额/可支付渠道/倒计时）。

        进入收银台前的前端调用，返回 payAmount / channels / leftTime(ms)。
        channels.type: 3=支付宝 6=微信（实测本环境返回 [6,3]）。
        """
        return self._request(
            "GET",
            f"/v1/unifiedPay/payDetail?storeId={env.store_id}"
            f"&subjectId={order_id}&subjectType=4",
        )

    def is_paid(self, order_id) -> bool:
        """GET /v1/unifiedPay/paySuccess —— 轮询"支付是否成功"。

        注意区分两个 paySuccess：
            /v1/unifiedPay/paySuccess  统一支付（C 端订单在用）  ← 本方法
            /v1/miguPay/paySuccess     咪咕支付侧的同名接口
        """
        data = self._request(
            "GET",
            f"/v1/unifiedPay/paySuccess?storeId={env.store_id}"
            f"&subjectId={order_id}&subjectType=4",
        )
        return bool(data.get("success")) if isinstance(data, dict) else False

    def force_sync_pay(self, org_transaction_id: str) -> dict:
        """POST /v1/miguPay/test/forceSyncPay —— 手动触发支付结果补单。

        等价于 xxl-job 的 MiguPayResultQueryJob：拿 orgTransactionId 主动查
        Ruby 3006，查到已支付就把订单补成"已支付"。
        用途：真实付款成功后，若网关回调没到（或本地环境收不到回调），
        用这个接口把结果补齐，让链路能跑完。
        """
        return self._request(
            "POST", f"/v1/miguPay/test/forceSyncPay?storeId={env.store_id}",
            json={"orgTransactionId": org_transaction_id},
        )

    def wait_for_paid(self, order_id, timeout: int = 300, interval: int = 3,
                      verbose: bool = True, sync_org_txn: str | None = None) -> bool:
        """轮询等待订单支付成功，返回 True/False（不抛异常，交给用例断言）。

        - 先查 /v1/unifiedPay/paySuccess；失败则再看订单 status 兜底
        - 每 30 秒尝试一次 forceSyncPay 补单（区别于每次都触发，避免刷爆日志）
        - 订单被自动关闭（status=4）会立刻返回 False，不再空等
        """
        import time

        deadline = time.time() + timeout
        n = 0
        while time.time() < deadline:
            n += 1
            if self.is_paid(order_id):
                return True

            detail = self.get_order_detail(order_id)
            status = detail.get("status")
            if status in PAID_STATUS:
                return True
            if status == STATUS_CLOSED and n > 2:
                # 前两次可能刚好在回调落地前，多给两轮机会再判死
                if verbose:
                    print(f"  订单已关闭(status={status})，停止等待")
                return False

            if sync_org_txn and n % 10 == 0:
                try:
                    r = self.force_sync_pay(sync_org_txn)
                    if verbose:
                        print(f"  第 {n} 轮：触发补单 -> {r}")
                except MiguApiError as e:
                    if verbose:
                        print(f"  第 {n} 轮：补单接口返回异常 {e}")

            if verbose and n % 5 == 0:
                left = int(deadline - time.time())
                print(f"  等待支付中… 第 {n} 轮，订单状态={status}，剩余 {left}s")
            time.sleep(interval)
        return False

    def force_sync_all(self) -> dict:
        """POST /v1/miguPay/test/forceSyncAll —— 全量补单（不传参）。

        扫描 hm_miguPaymentRecord 里所有 status=0 的记录逐个查 Ruby 3006。
        不知道 orgTransactionId 时的兜底手段。
        """
        return self._request(
            "POST", f"/v1/miguPay/test/forceSyncAll?storeId={env.store_id}", json={}
        )

    def get_eticket(self, order_id) -> dict:
        """GET /v1/order/{id}/eticket —— 订单电子票（核销码/二维码）。

        支付成功后的终点：能拿到电子票 = C 端闭环完成。
        真正的扫码核销在 B 端（后台核销台），C 端只负责出票。
        """
        return self._request(
            "GET", f"/v1/order/{order_id}/eticket?storeId={env.store_id}"
        )

    # ============ 以下为 930 回归用例（回归用例.xlsx）补的 C 端能力 ============
    # 全部为**新增方法**，不改动上面既有方法的签名与语义。
    # 逆向来源：生产 H5 bundle https://ticket-static.miguvideo.com/saishi/js/app.*.js
    #           及其 164 个懒加载 chunk（用 `.get("/v1/...")` / `.post(...)` 全量提取）。

    def try_call(self, method: str, path: str, *, auth: bool = True, **kwargs) -> dict:
        """与 `_request` 相同的请求，但**不抛异常**，返回完整响应体。

        负向用例（断言「非法输入被拒」）需要拿到 code/message 而不是异常，
        用它比 try/except MiguApiError 干净。返回形如
            {"code": 500, "message": "证件号不能为空", "data": ...}
        网络层异常仍会抛出（那是环境问题，不该被当成业务结论）。
        """
        if auth and self._hmcer is None:
            self.login()
        elif auth and not self.session.cookies.get("hmcer"):
            self._apply_hmcer()
        resp = self._send(method, f"{env.api_base}{path}", **kwargs)
        try:
            return resp.json()
        except ValueError:
            return {"code": resp.status_code, "raw": resp.text[:300]}

    # ---------------- 收货地址（纸质票「仅快递」下单前置） ----------------

    def get_addresses(self) -> list:
        """GET /v1/address —— 收货地址簿（`data.addresses[]`）。"""
        data = self._request("GET", f"/v1/address?storeId={env.store_id}")
        return (data or {}).get("addresses") or []

    def default_address(self) -> dict:
        """取一个可用收货地址；地址簿为空时抛断言错误（用例据此 skip）。"""
        addrs = self.get_addresses()
        assert addrs, ("C 端地址簿为空，无法下「仅快递」订单 —— "
                       "请先在 H5「我的-收货地址」里添加一条")
        for a in addrs:
            if a.get("defaultAddress"):
                return a
        return addrs[0]

    # ---------------- 实名人（观演人） ----------------

    def list_real_names(self, ids: list | None = None) -> list:
        """POST /v1/user/realNameInfo/search —— 观演人列表（不传 ids 即全量）。"""
        data = self._request(
            "POST", f"/v1/user/realNameInfo/search?storeId={env.store_id}",
            json={"ids": ids or []},
        )
        return (data or {}).get("data") or []

    def save_real_name(self, name: str, id_card_no: str,
                       id_card_type: str = "ID_CARD", country: str = "中国") -> dict:
        """POST /v1/user/realNameInfo/save —— 新增/编辑观演人（实名认证）。

        ★ 生产实测：后端会做**二要素核验**（姓名 + 身份证号送外部征信平台），
          随便编的号一律返回 `code=500 姓名与身份证号不一致，请核对后重试` /
          `身份信息核验失败，请核对后重试`。所以正向用例必须用真实身份，
          自动化只能覆盖「字段级校验」的负向分支。
        idCardType 取值（前端枚举）：ID_CARD=1 / PASS_PORT=2 / HONGKONG_PASS=3 /
          TAIWAN_PASS=4 / HMT_RESIDENCE_PERMIT=5 /
          FOREIGNERS_PERMANENT_RESIDENCE_PERMIT=6 / OTHER=7。
        """
        return self._request(
            "POST", f"/v1/user/realNameInfo/save?storeId={env.store_id}",
            json={"name": name, "idCardType": id_card_type,
                  "idCardNo": id_card_no, "country": country},
        )

    def delete_real_name(self, real_name_id) -> dict:
        """DELETE /v1/user/realNameInfo/{id} —— 删除观演人（用例收尾清理用）。"""
        return self._request(
            "DELETE", f"/v1/user/realNameInfo/{real_name_id}?storeId={env.store_id}"
        )

    # ---------------- 选座 ----------------

    def get_seat_map(self, product_id: int, event_id) -> dict:
        """GET /v1/seat/event/{eventId} —— C 端座位图。

        返回 `{productSkuList:[{skuId,priceName,price,stockNum}],
               seatAreaList:[{areaId,areaName,seatList:[{seatId,seatName,rowName,
                              saleStatus,enable,color,ticket{priceName,price}}]}]}`
        `saleStatus=0 且 enable=true` 才是可售座位。
        """
        return self._request(
            "GET", f"/v1/seat/event/{event_id}?storeId={env.store_id}"
                   f"&hmProductId={product_id}"
        )

    def free_seats(self, product_id: int, event_id, price=None, limit: int = 1) -> list:
        """挑可售座位；price 传数值则只挑该价位的座位（0 元票档最省事）。"""
        seats = []
        for area in (self.get_seat_map(product_id, event_id).get("seatAreaList") or []):
            for s in area.get("seatList") or []:
                if s.get("saleStatus") != 0 or not s.get("enable"):
                    continue
                if price is not None and float((s.get("ticket") or {}).get("price", -1)) != float(price):
                    continue
                seats.append(s)
                if len(seats) >= limit:
                    return seats
        return seats

    # ---------------- 下单（扩展：快递/自取 + 选座 + 实名人） ----------------

    def make_order(self, product_id: int, sku_id: int, count: int = 1,
                   total_amount: str = "0.00", *, delivery_type: int = 4,
                   address_id=None, seat_ids: list | None = None,
                   real_name_infos: list | None = None,
                   contact_name: str = "自动化测试",
                   contact_mobile: str | None = None,
                   source: str = "") -> int:
        """POST /v2/order/makeOrder —— 扩展版下单，返回 orderId。

        与 `create_order()` 的区别（`create_order` 保持原样，只支持电子票）：
          · `delivery_type`：4 电子票 / 1 快递（**必须**配 `address_id`）/ 3 自取
          · `address_id`：快递类必传（实测缺了报 `地址不能为空`；
            传 `userAddressId` 这个名字同样报错，字段名就是 `addressId`）
          · `seat_ids`：选座商品必传（缺了报 `选座商品请选择座位`）
          · `real_name_infos`：实名类商品的观演人（`realNameInfoList`），
            一票一证/一单一证商品缺了会被下单校验拦下
        仍然走清单白名单 `catalog.guard_order`。
        """
        catalog.guard_order(product_id, sku_id, source=source)
        delivery = {"type": delivery_type, "name": contact_name,
                    "mobileNo": contact_mobile or self.phone}
        if address_id is not None:
            delivery["addressId"] = address_id
        body = {
            "hmProductId": product_id,
            "skuId": sku_id,
            "count": count,
            "postage": 0,
            "totalAmount": total_amount,
            "channel": 2,
            "delivery": delivery,
            "promotionAmount": 0,
        }
        if seat_ids:
            body["seatIds"] = list(seat_ids)
        if real_name_infos:
            body["realNameInfoList"] = list(real_name_infos)
        data = self._request(
            "POST", f"/v2/order/makeOrder?storeId={env.store_id}", json=body
        )
        return data["orderId"]

    def confirm_order(self, sku_id, count: int = 1, product_id=None,
                      seat_ids: list | None = None) -> dict:
        """GET /v2/order/confirm —— 下单确认页（金额/交付方式/实名要求）。

        `seatIds` 是**逗号拼接**的字符串（前端就是这么传的）。
        `realNameLimit{needIdCard,count}` 决定要不要传观演人。
        """
        params = {"storeId": env.store_id, "skuId": sku_id, "count": count}
        if product_id is not None:
            params["hmProductId"] = product_id
        if seat_ids:
            params["seatIds"] = ",".join(str(x) for x in seat_ids)
        return self._request("GET", "/v2/order/confirm", params=params)

    def receive_order(self, order_id, purchase_order_id) -> dict:
        """POST /v1/order/{id}/received —— C 端「确认收货」（快递类）。

        实测：3(待收货) → 6(待观演)。
        """
        return self._request(
            "POST", f"/v1/order/{order_id}/received?storeId={env.store_id}",
            json={"purchaseOrderId": purchase_order_id},
        )

    def cancel_order(self, order_id, reason: str = "用户取消") -> dict:
        """POST /v1/order/{id}/cancel —— 取消未支付订单（收尾清理用）。

        ★ `reason` 是**必填 query 参数**（OpenAPI 里 required=true）。
          早期实现只发空 body、不带 reason，会拿到
          `400 Required String parameter 'reason' is not present`。
        """
        return self._request(
            "POST", f"/v1/order/{order_id}/cancel",
            params={"storeId": env.store_id, "reason": reason}, json={}
        )

    # ---------------- C 端条件退（TC-005~008 的「申请退款」步骤） ----------------
    #
    # 退款共三种方式，这是第 ③ 种（另两种在 migu_api/refund.py）：
    #   ① B端条件退 —— 订单详情发起，可选仅退票/仅退款/退票退款
    #   ② B端常规退 —— 退款中心发起 + 退款审批通过，只能全量退票退款
    #   ③ C端条件退 —— 就是下面这两个接口：用户自助、**无需审批**

    def refund_pre(self, order_id) -> dict:
        """GET /v1/order/refund/{id}/pre —— C 端条件退预校验（**只读**）。

        返回 `{canRefund, canShowRefund, refundTip, refundAmount, refundFeeAmount}`。
        **C 端条件退有三个并列的硬约束**，缺一不可（2026-09-30 生产 888 实测原文）：

        a. `canShowRefund` —— **场次是否配置了条件退**。未配置的场次
           refundTip「本项目不支持退款」，C 端**连退款入口都不显示**。
           实测：条件退场次 3241 的单 `canShowRefund=true`；
                 非条件退场次 3235 的单 `canShowRefund=false`。
        b. `canRefund` —— **订单是否已到终态**（已核销/已收货/已完成）。
           终态时 refundTip 变成「订单已经完成啦～如需退款，联系客服为您处理哦！」
           ——这是产品设计（终态不开放自助退），此时退款要走 B 端条件退/常规退。
        c. **退款金额不能为 0** —— 这条不在 pre 里体现（0 元单的 pre 依旧
           canRefund=true、refundAmount=0），而是在 submit 时拦截：
           400「0元单不可退款,请联系客服~」。
           ⚠️ 所以 **0 元票档夹具天然跑不了 C 端条件退**，别把"提交失败"
           误判成"功能有 bug"。

        `canShowRefund` 判"有没有入口"、`canRefund` 判"能不能点"，两者都真才可提交。
        """
        return self._request(
            "GET", f"/v1/order/refund/{order_id}/pre?storeId={env.store_id}"
        )

    def refund_submit(self, order_id, refund_amount=None, reason: str = "",
                      refund_type: int = 1) -> dict:
        """POST /v1/order/refund/{id}/submit —— C 端条件退提交（**无需审批**）。

        body `OrderRefundRequest{refundAmount, reason, operatorId, type}`。
        仅在 `refund_pre()` 的 `canShowRefund` 与 `canRefund` 都为真时才可能成功；
        `refund_amount` 传 0 会被后端拒绝（400「0元单不可退款,请联系客服~」）。
        """
        body: dict = {"reason": reason, "type": refund_type}
        if refund_amount is not None:
            body["refundAmount"] = float(refund_amount)
        return self._request(
            "POST", f"/v1/order/refund/{order_id}/submit?storeId={env.store_id}",
            json=body,
        )

    def refund_rules(self, product_id, event_id=None) -> dict:
        """GET /v1/activity/tips/refundRules —— C 端「退款规则」文案（只读）。

        返回 `{refundSupport, refundRuleConfigStatus, hasSingleEventRule,
        refundTip, refundRuleList[]}`。
        ⚠️ 实测这个接口是**商品级**的：传不同 eventId 仍返回同一个 bizId，
        所以**不能**用它区分"某场次有没有配条件退" —— 那个判断要用
        `refund_pre(order_id)["canShowRefund"]`（订单级，才是真正的入口判据）。
        """
        params = {"storeId": env.store_id, "productId": int(product_id)}
        if event_id is not None:
            params["eventId"] = int(event_id)
        return self._request("GET", "/v1/activity/tips/refundRules", params=params)

    # ---------------- 抢票准备 / 抢票提醒（TC-011 / TC-017） ----------------

    def set_prefabricate_real_names(self, product_id, ids) -> dict:
        """POST /v1/prefabricate/product/realNameInfo —— 抢票准备：预填观演人。

        body `PrepareProductBindRealNameSaveRequest{productId, realNameInfoIds[]}`。
        预填后，开售下单确认页会**自动带入**这些人 —— 就是 TC-011
        「带入信息与抢票准备预填信息一致」要验的链路。
        """
        return self._request(
            "POST", f"/v1/prefabricate/product/realNameInfo?storeId={env.store_id}",
            json={"productId": int(product_id),
                  "realNameInfoIds": [int(i) for i in ids]},
        )

    def set_prefabricate_address(self, product_id, address_id) -> dict:
        """POST /v1/prefabricate/product/userAddress —— 抢票准备：预填收货地址。

        body `PrepareProductBindAddressSaveRequest{productId, userAddressId}`。
        仅纸质票（快递）项目有意义。
        """
        return self._request(
            "POST", f"/v1/prefabricate/product/userAddress?storeId={env.store_id}",
            json={"productId": int(product_id), "userAddressId": int(address_id)},
        )

    def get_prefabricate_config(self, product_id) -> dict:
        """GET /v1/prefabricate/product/config —— 抢票准备（预填）已保存的内容。"""
        return self._request(
            "GET", f"/v1/prefabricate/product/config?productId={product_id}"
                   f"&storeId={env.store_id}"
        )

    def get_prefabricate_message(self, product_id) -> bool:
        """GET /v1/prefabricate/product/message —— 是否已订阅开售提醒。"""
        return bool(self._request(
            "GET", f"/v1/prefabricate/product/message?productId={product_id}"
                   f"&storeId={env.store_id}"
        ))

    def set_prefabricate_message(self, product_id, sub_status: bool) -> dict:
        """POST /v1/prefabricate/product/message —— 订阅/取消「开售提醒」。

        实测返回 `{phone: "183****7528"}`（提示语里用它拼「接收提醒号码」）。
        """
        return self._request(
            "POST", f"/v1/prefabricate/product/message?storeId={env.store_id}",
            json={"productId": product_id, "subStatus": sub_status},
        )

    def logout(self) -> dict:
        """POST /v1/logout —— C 端退出登录。

        ⚠️ **会作废当前 hmcer**。生产环境的 hmcer 只能靠 `login --harvest`
          从本机浏览器 Cookie 库取，且通行证续期通道实测已失效
          （`.migu_state/prod/passport_cookies.json` 过期，refresh_hmcer 报
          「通行证会话已失效」）—— 一旦退出登录，自动化侧就**无法自愈**。
          所以回归里只做契约校验（接口存在 + 方法正确），不做破坏性调用。
        """
        return self._request("POST", f"/v1/logout?storeId={env.store_id}")


# ---------------- 命令行自测 ----------------
if __name__ == "__main__":
    c = MiguClient()
    print("== 店铺信息（免登录）")
    shop = c.get_shop_info()
    print(f"    店铺名: {shop.get('name')}")
    print("== 会员信息（登录态）")
    info = c.get_member_info()
    print(f"    手机号: {info.get('phone')}")
    print(f"    历史订单: {[o for o in info.get('orderInfos', []) if o.get('totalCount')]}")
