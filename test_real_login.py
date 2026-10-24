# -*- coding: utf-8 -*-
"""
test_real_login.py —— 第一条真实环境用例：登录态 + 会员信息

和教学版 test_core_flow.py 的区别：
    - FakeTicketService 换成了真实的 MiguClient（打真实测试环境）
    - 断言的是真实业务数据（手机号、店铺名）
    - Allure 报告里能看到每一步的真实耗时和响应数据

运行：
    .venv/bin/pytest test_real_login.py --alluredir=allure-results -v
    allure serve allure-results
"""
import allure
import pytest

from migu_api.c2login import C2LoginError
from migu_api.client import MiguClient
from migu_api.config import env

# 分组标记：冒烟（环境连通性，秒级）
pytestmark = [pytest.mark.smoke, pytest.mark.release_core]


@pytest.fixture(scope="module")
def client():
    """整个模块共用一个客户端实例：登录态刷新一次，多条用例复用。

    scope="module" 的意思：本文件里所有用例共享同一个 fixture 实例，
    避免每条用例都重复登录（真实环境登录是有成本的）。
    """
    c = MiguClient()
    yield c


@allure.feature("上线验证-冒烟")
@allure.story("环境连通性")
@allure.severity(allure.severity_level.BLOCKER)   # 连环境都不通，后面全白搭
@allure.title("环境可达且店铺信息正确（免登录探针）")
def test_shop_info(client):
    """最基础的一条：接口网关活着、店铺 ID 配对了。

    这是唯一一条**免登录**的用例，所以它同时也是切环境后的第一道自检：
        ./run.sh prod smoke   → 能过就说明生产域名/前缀/storeId 都配对了
    """
    with allure.step(f"请求店铺信息接口（storeId={env.store_id}，免登录）"):
        shop = client.get_shop_info()
        # attach 把真实响应存进报告，失败时不用复现就能看到现场
        allure.attach(str(shop), name="店铺响应", attachment_type=allure.attachment_type.TEXT)

    with allure.step("断言：店铺存在且有名称"):
        assert shop.get("name"), f"店铺名称为空，响应: {shop}"


@allure.feature("上线验证-冒烟")
@allure.story("登录链路")
@allure.severity(allure.severity_level.BLOCKER)   # 登录态挂了 = 所有需登录用例全部 no-go
@allure.title("通行证登录态换取业务凭证 hmcer 并查到会员信息")
def test_member_login(client):
    """验证完整登录链路：通行证 Cookie → ST token → hmcer → 业务接口。

    这条用例挂在哪一环，报告里一目了然：
      - refresh_hmcer 报错 → 通行证 Cookie 失效，需要重新浏览器登录
      - phone 断言失败    → 登录态被换成了别的用户（严重问题）

    生产环境默认跳过：生产登录走「手机号 + 真实验证码」（生产没有验证码日志），
    必须先由真人完成一次登录（把 hmcer 落到 .migu_state/prod/）。
    没有登录态时本用例**跳过而不是失败** —— 因为"没登录"是环境准备问题，
    不是被测系统的问题，报成失败会误导。
    """
    if not env.accounts:
        pytest.skip(
            f"{env.name} 环境未登记测试账号（config.py 的 {env.name.upper()}.accounts），"
            "登录方式需先确认"
        )

    with allure.step("确保登录态可用（缓存或自动刷新）"):
        try:
            client.login()
        except C2LoginError as e:
            # 只吞"压根没有登录材料"这一种情况；登录态串号等真问题仍会失败
            allure.attach(str(e), name="如何准备登录态")
            pytest.skip(
                f"环境 [{env.name}] 尚无可用登录态，需真人参与一次登录后重跑。\n{e}"
            )

    with allure.step("查询会员信息"):
        info = client.get_member_info()
        allure.attach(str(info), name="会员响应", attachment_type=allure.attachment_type.TEXT)

    with allure.step("断言：登录态对应的手机号是已登记的测试账号之一"):
        # 用 in 而不是 == 第一个账号：一个环境可能登记多个号（生产就有两个，
        # 其中一个短信通道当前不可用），只要落在白名单里就说明没串号。
        # 串号（登录态属于别的用户）依然会被这条断言拦住。
        got = info.get("phone")
        assert got in env.accounts, (
            f"登录态手机号 {got!r} 不在已登记账号 {list(env.accounts)} 里，"
            "可能登录态串号或凭证缓存错误"
        )
        allure.attach(f"实际登录账号：{got}", name="账号核对",
                      attachment_type=allure.attachment_type.TEXT)

    with allure.step("断言：会员数据结构完整（orderInfos 存在）"):
        assert isinstance(info.get("orderInfos"), list), "orderInfos 缺失，接口结构可能变了"
