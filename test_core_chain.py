# -*- coding: utf-8 -*-
"""
test_core_chain.py —— 上线验证核心链路：商品 → 下单 → 支付记录 → 支付结果

对应《上线验证核心流程清单》的主链路：
    登录 → 选商品/SKU → 创建订单 → 发起支付 → 支付结果

运行前准备（一次性）：
    浏览器登录一次通行证，Cookie 已缓存在 .migu_state/passport_cookies.json
    （client 会自动用它领 ST token 换 hmcer，并自动续期）

已知限制（截至 2026-09-29）：
    - 测试环境下单后约 2 分钟不支付会被自动关闭（status 4），所以下单→支付必须快速连续执行
    - 本文件是**无人值守回归**，支付只验证到"网关受理 + 支付结果查询语义"这一层；
      "订单真的变成已支付 + 电子票出票"需要真人扫码付款，见 test_real_pay.py
      （动态开启：--real-pay=on，或 MIGU_REAL_PAY=1）
    - 已支付订单状态是 11（待使用），不是常见的 2/3/5 —— 见 client.py 的状态字典
"""
import allure
import pytest

from conftest import login_or_skip
from migu_api import catalog, paypage
from migu_api.client import MiguClient, MiguApiError, STATUS_UNPAID
from migu_api.config import env, prod_write_blocked, prod_write_skip_reason

# 下单目标**不再写死商品 ID**，而是从「测试专用商品清单」按用途取：
#   usage=paid → 全清单唯一的"主支付目标"（测试 P01 多次核销场次 / 生产 888 店 0.01 元电子票）
# 清单里没有 → 本模块自动跳过。换商品 = 改 catalog.json，不用碰代码。
try:
    _CHAIN_TARGET = catalog.one(usage="paid")
    PRODUCT_ID, SKU_ID, TOTAL_AMOUNT = (
        _CHAIN_TARGET.product_id, _CHAIN_TARGET.sku_id, _CHAIN_TARGET.price)
except catalog.CatalogError:
    _CHAIN_TARGET = None
    PRODUCT_ID, SKU_ID, TOTAL_AMOUNT = (None, None, "0.01")

pytestmark = [
    pytest.mark.chain,
    pytest.mark.release_core,
    # 生产写操作闸门：本文件会真的下单，生产默认拦掉
    pytest.mark.skipif(prod_write_blocked(), reason=prod_write_skip_reason()),
    pytest.mark.skipif(
        _CHAIN_TARGET is None,
        reason=f"环境 [{env.name}] 的「测试专用商品清单」(catalog.json) 里没有 "
               "usage=paid 的可下单目标 —— 先在 B 端建好商品，再执行 "
               "./run.sh catalog add 登记（或 ./run.sh catalog list 看现有清单）",
    ),
]

SOURCE = "test_core_chain"


@pytest.fixture(scope="module")
def client():
    """整个模块共用一个客户端（登录态一次，后续自动续期）。"""
    c = MiguClient()
    login_or_skip(c, context="核心链路 C 端登录态")
    return c


@allure.feature("核心链路")
@allure.story("商品与库存")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("测试商品存在且在售，SKU 可购买")
def test_product_on_sale(client):
    """上线验证前提：测试商品可购。挂了 = 测试环境数据有问题，先修数据再跑链路。"""
    with allure.step("确认下单目标来自「测试专用商品清单」"):
        allure.attach(
            f"{_CHAIN_TARGET.label}\n"
            f"用途标签：{', '.join(sorted(_CHAIN_TARGET.usages))}\n"
            f"清单文件：{catalog.current().source}",
            name="清单登记", attachment_type=allure.attachment_type.TEXT)

    with allure.step("查询商品详情"):
        product = client.get_product_detail(PRODUCT_ID)
        allure.attach(str(product.get("productName")), name="商品名")
    assert product.get("productName"), "商品详情为空"

    with allure.step("查询 SKU 列表并校验目标票档"):
        skus = client.get_sku_list(PRODUCT_ID)
        alive = [s for s in skus if not s.get("expired") and int(s.get("count") or 0) >= 1]
        allure.attach(
            "\n".join(f"skuId={s.get('skuId')} 库存={s.get('count')} 过期={s.get('expired')}"
                      for s in skus),
            name="SKU 明细",
        )
    assert any(s["skuId"] == SKU_ID for s in alive), f"SKU {SKU_ID} 不可用（过期或无库存）"


@allure.feature("核心链路")
@allure.story("下单-支付")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("创建订单后订单状态为待支付")
def test_create_order(client):
    """核心断言：下单成功且订单可查、状态=1（待支付）、金额正确。"""
    with allure.step(f"创建订单：商品 {PRODUCT_ID} / SKU {SKU_ID} / 1 张"):
        order_id = client.create_order(PRODUCT_ID, SKU_ID, 1, TOTAL_AMOUNT,
                                       source=SOURCE)
        allure.attach(str(order_id), name="orderId")

    with allure.step("查询订单详情并校验状态与金额"):
        detail = client.get_order_detail(order_id)
        allure.attach(
            f"status={detail.get('status')} paymentPrice={detail.get('paymentPrice')}",
            name="订单快照",
        )
    assert detail.get("status") == 1, f"期望待支付(1)，实际 {detail.get('status')}"
    assert float(detail.get("paymentPrice") or 0) == float(TOTAL_AMOUNT), "订单金额不符"


@allure.feature("核心链路")
@allure.story("下单-支付")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("发起支付：创建支付记录并向网关请求支付参数")
def test_create_payment(client):
    """验证收银台入口可用：miguPay/pay 能创建支付记录并拿到支付参数。

    注意：下单后不支付会很快被自动关闭，所以这条用例自己下一单自己付，
    不依赖上一条用例留下的订单。
    """
    with allure.step("创建订单"):
        order_id = client.create_order(PRODUCT_ID, SKU_ID, 1, TOTAL_AMOUNT,
                                       source=SOURCE)

    with allure.step("创建支付记录（/v1/miguPay/pay）"):
        pay = client.create_payment(order_id)
        allure.attach(str(pay)[:300], name="支付参数")

    with allure.step("校验：拿到了可支付的参数（链接或表单，随渠道而定）"):
        # 渠道不同返回形态就不同（paypage.py 有说明）：
        #   微信 WX → https://wx.tenpay.com/... 纯链接
        #   支付宝 AP → <![CDATA[<form action="https://openapi.alipay.com/...">]]> HTML 表单
        # 生产只开支付宝，所以这里绝不能只认 startswith("http")（曾因此在生产误报失败）。
        raw = pay.get("paymentParams") or ""
        assert raw, "未返回支付参数（paymentParams 为空）"
        assert paypage.is_link(raw) or paypage.is_form(raw), (
            f"支付参数既不是链接也不是表单：{raw[:120]!r}"
        )
        allure.attach(f"渠道 {env.pay_bank_code} → {paypage.describe(raw)}",
                      name="支付参数形态",
                      attachment_type=allure.attachment_type.TEXT)
        # 附一条"人能付的链接"：表单形态还原成等价 GET 链接，方便出二维码
        allure.attach(raw if paypage.is_link(raw) else paypage.to_pay_url(raw),
                      name="可直接打开的支付链接", attachment_type=allure.attachment_type.TEXT)


@allure.feature("核心链路")
@allure.story("下单-支付")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("支付结果查询：未支付订单不得报「已支付」（防假阳性）")
def test_pay_result_not_paid_before_payment(client):
    """把 `/v1/unifiedPay/paySuccess` 的语义钉死：**没付钱就不能说已付**。

    为什么值得一条独立用例：前端"付款成功"的判断完全依赖这个探针，
    它一旦在未支付时误报 `success=true`，用户就会看到"支付成功"却拿不到票。
    反向的问题我们也真实遇到过（2026-09-29 生产 61008472：**真付了钱、订单却
    一直待付款**，回调丢失）—— 两个方向都说明这个探针的语义必须有人守着。

    前置状态刻意构造成"最容易骗过探针"的样子：**已经创建了支付凭证但没付款**。
    做法：下一笔真单 + 建支付凭证 → 查支付结果必须不为 true，且订单仍是 1(待付款)。
    （本来这条是用 `/v1/miguPay/test/pay` 调试后门验"网关协议通"的，但该后门
      2026-09-29 被发版整组摘掉、生产上又挂死；改用真实接口 `miguPay/pay`
      做等价且更有意义的覆盖，本条不再需要跳过。）
    """
    with allure.step(f"创建订单：商品 {PRODUCT_ID} / SKU {SKU_ID} / {TOTAL_AMOUNT} 元"):
        order_id = client.create_order(PRODUCT_ID, SKU_ID, 1, TOTAL_AMOUNT,
                                       source=SOURCE)
        allure.attach(str(order_id), name="orderId")

    with allure.step("创建支付凭证（有凭证但**没有付款**）"):
        pay = client.create_payment(order_id)
        params = pay.get("paymentParams") or ""
        allure.attach(f"渠道 {env.pay_bank_code} → {paypage.describe(params)}",
                      name="支付凭证形态")
    assert params, f"未拿到支付参数，构造不出「已建凭证未付款」的前置状态：{pay}"

    with allure.step("查支付结果：paySuccess 必须不为 true"):
        # 两种合法表现都算"没付钱"：
        #   ① 正常返回 {"success": false}
        #   ② 接口明确拒绝（code!=200，例如"不存在的支付凭证"）→ 抛 MiguApiError
        # 只有 success=true 才是假阳性。
        try:
            paid_signal = client.is_paid(order_id)
            detail = f"paySuccess = {paid_signal}"
        except MiguApiError as exc:
            paid_signal = False
            detail = f"接口拒绝（等价于未支付）：{exc}"
        allure.attach(detail, name="支付结果探针")

    assert paid_signal is False, (
        f"订单 {order_id} 尚未付款，支付结果查询却报已支付 —— "
        "前端会据此显示「支付成功」，用户拿不到票（典型的假阳性）"
    )

    with allure.step("交叉校验：订单状态仍应是待付款(1)"):
        detail_order = client.get_order_detail(order_id)
        allure.attach(f"status={detail_order.get('status')} "
                      f"paymentPrice={detail_order.get('paymentPrice')}", name="订单快照")
    assert detail_order.get("status") == STATUS_UNPAID, (
        f"未付款订单状态应为待付款({STATUS_UNPAID})，"
        f"实际 {detail_order.get('status')}"
    )


@allure.feature("核心链路")
@allure.story("下单-支付")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("重复下单/异常参数被正确拒绝")
def test_order_negative(client):
    """反例：无效 SKU 下单必须被拒绝（防呆校验是上线质量的一部分）。

    两道防线，任一拦住都算通过：
      ① **清单白名单**（client.create_order 内置）—— C 端下单只允许落在
         catalog.json 登记的项目·场次·票档上，非法 SKU 在本地就被拦；
      ② 服务端校验 —— 万一清单里放行了、服务端仍应拒绝无效 SKU。
    所以这里同时接受 CatalogViolation 与 MiguApiError。
    """
    with allure.step("用不存在的 SKU 下单，期望被拒（白名单 / 服务端）"):
        with pytest.raises((MiguApiError, catalog.CatalogViolation)) as ei:
            client.create_order(PRODUCT_ID, sku_id=1, total_amount=TOTAL_AMOUNT,
                                source=SOURCE)
        allure.attach(f"{type(ei.value).__name__}: {str(ei.value)[:400]}",
                      name="拒绝原因", attachment_type=allure.attachment_type.TEXT)
