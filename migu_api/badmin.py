# -*- coding: utf-8 -*-
"""
badmin.py —— B 端（商家后台 / 核销台）客户端

为什么 C 端跑通了还要这个：
    "核销"这个动作只存在于 B 端。C 端只能出票（拿到券码），
    真正把券码核销掉、把订单推到"已完成"的，是 B 端核销台。
    所以完整的上线验证链路 = C端(登录→下单→支付→出票) + B端(核销→订单已完成)。

登录方式（实测 2026-09-29，与 C 端完全不同）：
    C 端：通行证会话 → ST token → hmcer Cookie
    B 端：4A 单点登录，浏览器地址栏带着 ?iamcaspticket=xxx
          但票据不能直接当 Cookie 用，必须换会话：
              GET {b_api}/v1/user/ssoCallback?iamcaspticket=xxx
              → Set-Cookie: hmid/ll/ct/dvid（这就是 B 端登录态）
    票据**不是一次性的**（2026-09-29 实测：连兑 3 次都成功，且寿命以天计 ——
    生产票据放到 7.8 天后、测试 11.7 天后仍可兑换成功），但会过期。
    所以浏览器历史里的票据是可以事后拿来换会话的（见 bticket.cmd_renew）；
    拿到会话后落盘复用，避免每次都去要新票据。

鉴权要点：
    所有请求都要带 Cookie + 请求头 `hm-store-id: <storeId>`，缺了会 401 未授权的访问。

核销接口（从后端自带 OpenAPI 文档 /api/v3/api-docs 里挖出来的）：
    POST {b_api}/v1/shopTrade/order/eticket/verification
         body {"storeId": 1, "codeList": ["券码"]}     ← storeId 必须是数字，放 body
         成功: {"code":200,"message":"核销成功"}
         超限: {"code":400,"message":"已核销，无需重复核销"}

    ⚠️ 历史坑：前端 SDK 曾把整个参数对象塞进 storeId 字段 → 后端 10201 JSON parse error，
    看起来像"核销接口有缺陷"，其实是前端拼参数错了。按上面的结构直接调 API 完全正常。
"""
import json
from pathlib import Path

import requests
import urllib3

from .config import b_env, BROWSER_UA
from .fsutil import ensure_dir

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATE_DIR = Path(__file__).resolve().parent.parent / ".migu_state" / b_env.state_dir_name
# B 端会话缓存（按环境隔离：.migu_state/test/b_cookies.json、.migu_state/prod/b_cookies.json）
B_COOKIE_FILE = STATE_DIR / "b_cookies.json"


class BAdminError(AssertionError):
    """B 端业务码非 200 时抛出（继承 AssertionError，让 pytest 当断言失败看）。"""

    def __init__(self, method: str, path: str, status: int, body):
        self.status, self.body = status, body
        preview = json.dumps(body, ensure_ascii=False)[:300]
        super().__init__(f"{method} {path} -> HTTP {status}: {preview}")


class BAdminAuthError(BAdminError):
    """B 端登录态失效：需要用户重新从 4A 入口拿一张新票据。"""


class BAdminClient:
    """B 端商家后台客户端。"""

    def __init__(self, ticket: str | None = None, store_id: str | None = None,
                 cookie_file: Path | None = None):
        self.store_id = store_id or b_env.store_id
        self.cookie_file = Path(cookie_file or B_COOKIE_FILE)
        self.ticket = ticket
        self.session = self._build_session()
        self._load_cookies()

    # ---------------- 基础设施 ----------------

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "hm-store-id": self.store_id,
            # Referer 要用前端真实路径：测试 http://ticket-test.../，生产 https://ticket.miguvideo.com/bfront/
            "Referer": b_env.front_url + "/",
            "User-Agent": BROWSER_UA,
            "Content-Type": "application/json",
        })
        # 生产证书是正规的，要校验；测试环境是 http，不受影响
        if b_env.base_url.startswith("https://"):
            s.verify = True
        # 本机系统代理会把这套内网域名带偏，B 端一律直连
        s.proxies = {"http": None, "https": None}
        return s

    def _load_cookies(self) -> None:
        """复用上次登录落盘的 Cookie（票据会过期，能复用就复用）。"""
        if not self.cookie_file.exists():
            return
        try:
            for c in json.loads(self.cookie_file.read_text(encoding="utf-8")):
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                                         path=c.get("path", "/"))
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    def _save_cookies(self) -> None:
        ensure_dir(self.cookie_file.parent)
        self.cookie_file.write_text(
            json.dumps([{"name": c.name, "value": c.value, "domain": c.domain,
                         "path": c.path} for c in self.session.cookies],
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def login(self, ticket: str | None = None) -> dict:
        """用 4A 票据换 B 端会话（换完落盘复用）。

        ticket 不传时读环境变量 MIGU_B_TICKET。
        返回用户信息（含 roles，判断能否核销）。

        注意：票据可重复兑换、寿命以天计，所以同一张票多在几处用没问题；
        但失败时**不会**覆盖已落盘的 Cookie（_request 抛异常时就返回了）。
        """
        import os

        ticket = ticket or self.ticket or os.environ.get("MIGU_B_TICKET")
        if not ticket:
            raise BAdminAuthError(
                "GET", "/v1/user/ssoCallback", 0,
                {"hint": "缺少 4A 票据：传参 ticket= 或设环境变量 MIGU_B_TICKET "
                         "（从 B 端入口 URL 的 ?iamcaspticket= 里取）"},
            )
        data = self._request("GET", "/v1/user/ssoCallback",
                             params={"iamcaspticket": ticket}, auth=False)
        self._save_cookies()
        return data

    def _request(self, method: str, path: str, *, auth: bool = True, **kwargs) -> dict:
        url = f"{b_env.api_base}{path}"
        resp = self.session.request(method, url, timeout=b_env.verify_timeout, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            raise BAdminError(method, path, resp.status_code, {"raw": resp.text[:300]})

        code = body.get("code")
        if auth and code == 401:
            raise BAdminAuthError(method, path, resp.status_code, body)
        if code != 200:
            # 业务失败（如 400 已核销）保留原始 body，用例里按需断言 message
            err = BAdminError(method, path, resp.status_code, body)
            err.code, err.message = code, body.get("message")
            raise err
        return body.get("data", body)

    # ---------------- 订单与票据查询 ----------------

    def list_orders(self, keyword: str | None = None, order_status: int = 0,
                    page_size: int = 10, page_index: int = 1) -> dict:
        """GET /v1/shopTrade/order —— B 端订单列表（keyword 传订单号可精确查单车）。"""
        params = {"storeId": self.store_id, "parentCategoryId": 1,
                  "orderStatus": order_status, "pageSize": page_size,
                  "pageIndex": page_index}
        if keyword:
            params["keyword"] = str(keyword)
        return self._request("GET", "/v1/shopTrade/order", params=params)

    def get_order(self, order_id) -> dict | None:
        """按订单号在列表里精确查一条，查不到返回 None（新单进检索索引有延迟）。"""
        data = self.list_orders(keyword=str(order_id), page_size=5)
        rows = data.get("data") or []
        for row in rows:
            if str(row.get("orderId")) == str(order_id):
                return row
        return None

    def get_eticket(self, order_id) -> dict:
        """GET /v1/shopTrade/order/{id}/eticket —— 电子票 + 核销次数 + 核销记录。

        返回里最关键的三兄弟：
            totalVerifyCount / usedVerifyCount / remainVerifyCount
        以及 verifyRecordList（每次核销的流水：序号/操作人/时间）。
        """
        data = self._request("GET", f"/v1/shopTrade/order/{order_id}/eticket",
                             params={"storeId": self.store_id})
        return data[0] if isinstance(data, list) and data else data

    def voucher_detail(self, code: str) -> dict | None:
        """GET /v2/shopTrade/order/verificationVoucher —— 按券码查券（含 maxVerifyCount）。

        注意：另一个同名的 /v1/verification/voucher 是「核销记录」查询，
        按 code 查流水；要查券本身（含次数上限）用这个 /v2 的。
        """
        data = self._request("GET", "/v2/shopTrade/order/verificationVoucher",
                             params={"keyword": code})
        return data[0] if isinstance(data, list) and data else None

    def wait_order(self, order_id, timeout: int = 40, interval: float = 3.0) -> dict | None:
        """等订单出现在 B 端列表里（新单进检索索引约需 3~10 秒）。"""
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            order = self.get_order(order_id)
            if order:
                return order
            time.sleep(interval)
        return None

    # ---------------- 纸质票履约：发货 / 确认收货（2026-09-30 实测打通） ----------------

    def deliver_info(self, order_id) -> dict:
        """GET /v1/shopTrade/order/{id}/deliverInfo —— 发货弹窗的前置数据。

        返回 `userAddress{id,name,phone,district,address}` + `deliverCompanies[]`。
        ★ `userAddress.id` 就是下面 `deliver()` 要的 `userAddressId` ——
          第一次做发货时踩过：只传 purchaseOrderId 会得到 `code=500 系统错误`，
          补上 userAddressId 才成功（company 必须是 deliverCompanies 里的取值）。
        """
        return self._request("GET", f"/v1/shopTrade/order/{order_id}/deliverInfo",
                             params={"storeId": self.store_id})

    def deliver(self, order_id, purchase_order_id=None, user_address_id=None,
                company: str = "", express_no: str = "") -> dict:
        """POST /v1/shopTrade/order/{orderId}/deliver —— B 端发货（快递类订单）。

        成功后 C 端订单 2(待发货) → 3(待收货)。

        三个入参的坑（都实测踩过）：
          · `userAddressId` —— 取 `deliver_info()['userAddress']['id']`（如 206）。
            **不能**用 C 端 `/v1/address` 的地址 id（那是 203425720547152，两套 id 不通）。
          · `company` —— 必须取 `deliver_info()['deliverCompanies']` 里的值。
          · `purchaseOrderId` —— 是**采购单号**，就在订单列表行的 `purchaseOrderId`
            字段里（如 722），**不是** orderId/unifiedOrderId！
            传 orderId 会得到 `code=500 系统错误`（后端不报字段名，极易误判成"接口有 bug"）。
            `purchase_order_id` 不传时本方法自动从订单列表行里取。
        """
        if purchase_order_id is None:
            row = self.get_order(order_id) or {}
            purchase_order_id = row.get("purchaseOrderId")
            if purchase_order_id is None:
                raise BAdminError(
                    "GET", f"/v1/shopTrade/order({order_id})", 200,
                    {"hint": "订单列表行里没有 purchaseOrderId，无法发货 —— "
                             "新单进检索索引有延迟，先 wait_order() 一下"},
                )
        if user_address_id is None:
            user_address_id = (self.deliver_info(order_id).get("userAddress") or {}).get("id")
        return self._request(
            "POST", f"/v1/shopTrade/order/{order_id}/deliver",
            json={"purchaseOrderId": purchase_order_id,
                  "userAddressId": user_address_id,
                  "company": company, "expressNo": express_no},
        )

    def receive(self, order_id, purchase_order_id=None) -> dict:
        """POST /v1/shopTrade/order/{orderId}/received —— B 端确认收货 / 自取核销。

        两个场景共用这一条（实测）：
          · 快递类：3(待收货) → 6(待观演)
          · 自取类：8(待自取) → 6(待观演)   ← 就是用例里的「商家端设置已取」
        注意与 `deliver()` 的区别：deliver 只做快递发货，对自取单会报「快递公司不能为空」。

        ⚠️ `purchaseOrderId` 同样是**采购单号**（列表行字段），传 orderId 会 `500 系统错误`。
        """
        if purchase_order_id is None:
            row = self.get_order(order_id) or {}
            purchase_order_id = row.get("purchaseOrderId")
            if purchase_order_id is None:
                raise BAdminError(
                    "GET", f"/v1/shopTrade/order({order_id})", 200,
                    {"hint": "订单列表行里没有 purchaseOrderId，无法确认收货"},
                )
        return self._request(
            "POST", f"/v1/shopTrade/order/{order_id}/received",
            json={"purchaseOrderId": purchase_order_id},
        )

    def real_name_search(self, keyword: str = "") -> list:
        """GET /v1/shopTrade/order/realNameSearch —— B 端按实名信息搜订单。

        PDA 核销按身份证搜单走的就是它（对应用例 TC-010「搜索票券码/身份证」）。
        实测：keyword 传订单号也能命中（同一检索入口）。
        """
        params = {"storeId": self.store_id, "pageSize": 10, "pageIndex": 1}
        if keyword:
            params["keyword"] = keyword
        data = self._request("GET", "/v1/shopTrade/order/realNameSearch", params=params)
        return data.get("data") if isinstance(data, dict) else (data or [])

    def order_event_log(self, order_id) -> list:
        """GET /v1/shopTrade/order/{orderId}/eventLog —— 订单操作流水（只读）。

        返回 `[{id, addTime, desc}]`，例如
            ["订单已提交，等待支付", "订单支付成功！", "订单已发货",
             "商家已备票完成，等待发货", "配送成功: 确认已收到票"]
        —— 这条流水是「状态流转"确实发生过"」最直接的证据，比只看当前 status 更硬。
        """
        data = self._request("GET", f"/v1/shopTrade/order/{order_id}/eventLog",
                             params={"storeId": self.store_id})
        return data if isinstance(data, list) else (data or {}).get("data") or []

    def verify_records(self, code: str, page_size: int = 10) -> list:
        """GET /v1/verification/voucher —— 核销记录查询（按券码）。"""
        data = self._request("GET", "/v1/verification/voucher",
                             params={"storeId": self.store_id, "code": code,
                                     "pageSize": page_size, "pageIndex": 1})
        return data.get("data") or []

    # ---------------- 核销动作 ----------------

    def verify(self, codes: str | list) -> dict:
        """POST /v1/shopTrade/order/eticket/verification —— 核销（支持一次多个券码）。

        storeId 必须是**数字**放在 body 里，codeList 只放券码字符串；
        把参数对象塞进 storeId 会得到 10201 JSON parse error（前端历史 bug）。
        成功返回 {"code":200,"message":"核销成功"}；达到次数上限返回 400 已核销。
        """
        if isinstance(codes, str):
            codes = [codes]
        return self._request(
            "POST", "/v1/shopTrade/order/eticket/verification",
            json={"storeId": int(self.store_id), "codeList": list(codes)},
        )

    def verify_result(self, codes: str | list) -> tuple[int, str]:
        """核销并返回 (code, message)，不抛异常 —— 便于断言失败场景（如超限）。"""
        try:
            self.verify(codes)
            return 200, "核销成功"
        except BAdminError as e:
            return getattr(e, "code", None) or e.status, getattr(e, "message", "") or ""


# ---------------- 命令行自测 ----------------
if __name__ == "__main__":
    import os

    c = BAdminClient()
    if os.environ.get("MIGU_B_TICKET"):
        print("== 用 4A 票据登录 ==")
        print("   用户:", c.login().get("userName"))
    print("== 会话自检 ==")
    print("   订单总数:", c.list_orders(page_size=1).get("totalNum"))
