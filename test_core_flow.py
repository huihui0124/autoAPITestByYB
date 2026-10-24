"""
上线验证核心流程 —— 第一条自动化用例（教学版）

这条用例模拟票务系统最核心的一条链路：

    造数(场次/票档) → 创建订单 → 支付成功 → 核销一次

并在每个阶段断言「库存、订单状态、核销次数」这三个业务结果。

真实项目里，你只需要把下面 FakeTicketService 的模拟实现
换成 requests 调真实测试环境接口，用例部分一行都不用改。

运行方式（在本目录下）：
    .venv/bin/pytest test_core_flow.py --alluredir=allure-results -v
看报告：
    allure serve allure-results
"""

import json

import allure
import pytest

# 分组标记：教学 demo（纯内存模拟，不打真实环境）
pytestmark = pytest.mark.demo


# ============================================================
# 第一部分：模拟的「被测系统」
# ------------------------------------------------------------
# 真实项目里没有这一段 —— 直接调接口即可。
# 这里用内存字典模拟后端行为，是为了让用例脱离环境也能跑，
# 把注意力集中在「用例本身怎么写」上。
# ============================================================
class FakeTicketService:
    """用内存字典模拟票务后端的最小实现"""

    def __init__(self, stock: int):
        self.stock = stock   # 票档剩余库存
        self.order = None    # 当前订单：None 或 dict

    def create_order(self, qty: int) -> dict:
        """创建订单：库存够才允许下单，下单后立即锁库存"""
        if qty > self.stock:
            raise ValueError("库存不足")
        self.stock -= qty                      # 业务规则：下单即锁库存
        self.order = {"qty": qty, "status": "待支付"}
        return self.order

    def pay_success(self) -> dict:
        """支付回调成功：订单流转为已出票"""
        self.order["status"] = "已出票"
        return self.order

    def verify_once(self) -> dict:
        """核销一次：累加核销次数"""
        self.order["verify_count"] = self.order.get("verify_count", 0) + 1
        return self.order


# ============================================================
# 第二部分：fixture —— 用例的「前置条件」
# ------------------------------------------------------------
# @pytest.fixture 装饰的函数 = 前置准备。
# 用例函数的参数名写上 fixture 名字，pytest 会自动把返回值传进来。
# 真实项目里，这里通常放「登录拿 token」「造测试场次」这类动作。
# yield 之前是前置，yield 之后是清理（本例没有需要清理的资源）。
# ============================================================
@pytest.fixture
def ticket_service() -> FakeTicketService:
    """准备一个库存 10 张的测试票档（相当于造数）"""
    with allure.step("前置：创建测试场次，票档库存 10 张"):
        svc = FakeTicketService(stock=10)
    yield svc


# ============================================================
# 第三部分：用例本体
# ------------------------------------------------------------
# 装饰器的含义（都会体现在 Allure 报告里）：
#   @allure.feature  → 报告左侧的功能模块分类（对应你用例表的「功能模块」列）
#   @allure.story    → 模块下的子流程
#   @allure.severity → 严重级别；BLOCKER 挂了 = 上线 no-go
#   @allure.title    → 报告里显示的用例标题（不写则默认用函数名）
# ============================================================
@allure.feature("核心链路")
@allure.story("下单-支付-核销")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("创建订单后库存-2，支付出票后可核销")
def test_order_pay_verify(ticket_service):
    """一条用例 = 一次上线验证检查，断言全部落在业务结果上"""

    # ---------- 步骤 1：创建订单 ----------
    # allure.step 会把 with 块内的耗时和结果单独记成报告里的一行，
    # 失败时能直接看到挂在哪一步，而不是只看到一个函数名。
    with allure.step("步骤1：创建订单，购买 2 张"):
        order = ticket_service.create_order(qty=2)

    # 断言写「业务结果」，不写「接口返回 200」——
    # 接口 200 但库存没扣，才是上线时真正要拦住的事故。
    assert order["status"] == "待支付"
    assert ticket_service.stock == 8, f"下单后库存应为 8，实际 {ticket_service.stock}"

    # allure.attach 把现场证据存进报告：定时任务半夜跑挂了，
    # 早上不用复现，展开用例就能看到当时的原始数据。
    # 注意：Python 版的 attach 没有便捷方法 .json()，
    # 要自己序列化后指定 attachment_type（这是新手最常踩的 API 差异）。
    allure.attach(
        json.dumps(order, ensure_ascii=False, indent=2),
        name="订单数据",
        attachment_type=allure.attachment_type.JSON,
    )

    # ---------- 步骤 2：支付成功 ----------
    with allure.step("步骤2：支付回调成功，订单出票"):
        order = ticket_service.pay_success()
    assert order["status"] == "已出票"

    # ---------- 步骤 3：核销 ----------
    with allure.step("步骤3：现场核销 1 次"):
        order = ticket_service.verify_once()

    # 断言核销次数：多次核销场景下这个数字是最容易被改错的
    assert order["verify_count"] == 1
