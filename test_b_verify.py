# -*- coding: utf-8 -*-
"""
test_b_verify.py —— 核销链路（C 端出票 → B 端核销 → 订单已完成）

这是"上线验证"闭环的最后一段：
    C 端：登录 → 下单 → （支付） → 出电子票
    B 端：核销台 → 核销 → 订单变已完成

为什么这条能做成全自动（不需要付款、不需要人工）：
    商品 222665「测试ff」是 **0 元电子票**，C 端 makeOrder 之后订单直接落到
    "待核销"(11) 并立刻出票（券码 + canConsume=true），天然就是核销用例的夹具。
    用户 B 端框架里用过的 222663 场次已结束，所以这里换成 222665。

B 端怎么登录（两套系统登法完全不同）：
    C 端 = 通行证会话 + ST token + hmcer Cookie（见 passport.py，浏览器登录一次就长期自动）
    B 端 = 4A 单点登录，需要一张一次性票据：
            1. 从 4A / 门户点进 B 端后台，地址栏会带 ?iamcaspticket=xxx
            2. 把票据给脚本：MIGU_B_TICKET='xxx' ./.venv/bin/pytest test_b_verify.py
            3. 票据是一次性的，脚本换完会话会缓存到 .migu_state/b_cookies.json，
               后续直接复用；会话过期再要一张新票据即可
    没有票据也没有缓存会话时，本文件会自动跳过（不会让回归变红）。

核销接口的正确姿势（踩坑记录）：
    POST {b_api}/v1/shopTrade/order/eticket/verification
    body {"storeId": 1(数字), "codeList": ["券码"]}
    ⚠️ 前端 SDK 曾把整个参数对象塞进 storeId 字段，报 code=10201 JSON parse error，
       看起来像"接口有缺陷"，其实是前端拼参错误 —— 直接调 API 完全正常（本文件即为证明）。
"""
import json
import os
import time
from pathlib import Path

import allure
import pytest

from conftest import login_or_skip
from migu_api import catalog
from migu_api.badmin import BAdminClient, BAdminAuthError, B_COOKIE_FILE, BAdminError
from migu_api.client import (MiguClient, STATUS_FINISHED, STATUS_PAID,
                             STATUS_UNPAID, status_text)
from migu_api.config import env, prod_write_blocked, prod_write_skip_reason

# 分组标记：核销链路（C端出票 → B端核销）
pytestmark = [
    pytest.mark.b_verify,
    pytest.mark.release_core,
    # 生产写操作闸门：本文件会真的核销券码（含"无效券码"反例也会打到核销接口），
    # 生产默认整组拦掉
    pytest.mark.skipif(prod_write_blocked(), reason=prod_write_skip_reason()),
]

# 0 元电子票商品：makeOrder 即终态（待核销），无需支付，可反复造夹具。
# ★ 目标**从「测试专用商品清单」按用途取**（usage=free_fixture），不写死 ID：
#     清单里没有 → skip，而不是随手拿别的商品下单（那就是"清单外下单"了）。
try:
    FREE_TARGET = catalog.one(usage="free_fixture")
    FREE_PRODUCT = (FREE_TARGET.product_id, FREE_TARGET.sku_id)
except catalog.CatalogError:
    FREE_TARGET, FREE_PRODUCT = None, None

SOURCE = "test_b_verify._make_free_order"

HAS_B_SESSION = B_COOKIE_FILE.exists() or bool(os.environ.get("MIGU_B_TICKET"))
skip_b = ("需要 B 端 4A 票据：MIGU_B_TICKET='<iamcaspticket>' "
          "./.venv/bin/pytest test_b_verify.py")
no_fixture = ("「测试专用商品清单」(catalog.json) 里没有 usage=free_fixture 的 0 元票档，"
              "无法自动造可核销夹具 —— 用 ./run.sh catalog list 看清单 / "
              "./run.sh catalog add 登记")


@pytest.fixture(scope="module")
def c_client():
    """C 端客户端（登录态自动续期）。"""
    c = MiguClient()
    login_or_skip(c, context="B端核销 C 端登录态")
    return c


@pytest.fixture(scope="module")
def b_client():
    """B 端客户端；有票据就先换一次会话。"""
    b = BAdminClient()
    if os.environ.get("MIGU_B_TICKET"):
        try:
            b.login()
        except BAdminAuthError as e:
            pytest.skip(f"B 端票据兑换失败（票据可能已用过/过期）：{e}")
    return b


def _make_free_order(c_client) -> tuple[int, str]:
    """造夹具：0 元电子票下单 → 返回 (orderId, 券码)。

    注意出票是**异步**的：下单成功那一刻可能还查不到票（会返回空数组），
    所以这里轮询等票出来，别拿"空票"当失败。
    """
    product_id, sku_id = FREE_PRODUCT
    order_id = c_client.create_order(product_id, sku_id, 1, FREE_TARGET.price,
                                     source=SOURCE)
    deadline = time.time() + 30
    while time.time() < deadline:
        ticket = c_client.get_eticket(order_id)
        items = ticket if isinstance(ticket, list) else [ticket]
        items = [t for t in items if t and t.get("code")]
        if items:
            return order_id, items[0]["code"]
        time.sleep(2)
    raise AssertionError(f"订单 {order_id} 下单后 30s 仍未出票")


@allure.feature("核销链路")
@allure.story("B端核销")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("B端订单查询可用：C端新单能在B端核销台查到")
@pytest.mark.skipif(not HAS_B_SESSION, reason=skip_b)
@pytest.mark.skipif(FREE_PRODUCT is None, reason=no_fixture)
def test_b_order_visible(c_client, b_client):
    """C 端下的单必须能被 B 端核销台检索到 —— 跨系统数据一致性，链路前提。"""
    with allure.step("C 端 0 元下单"):
        order_id, code = _make_free_order(c_client)
        allure.attach(f"orderId={order_id} code={code}", name="C端夹具")

    with allure.step("等 B 端检索索引收录（实测 3~10s）"):
        t0 = time.time()
        order = b_client.wait_order(order_id, timeout=40)
        cost = round(time.time() - t0, 1)
        allure.attach(f"耗时 {cost}s", name="索引延迟")

    assert order, f"B 端查不到订单 {order_id}（跨系统数据不一致）"
    with allure.step("校验 B 端订单状态与票种"):
        allure.attach(json.dumps(
            {k: order.get(k) for k in ("orderId", "status", "statusName", "statusText",
                                       "deliveryType", "deliveryTypeName", "paymentPrice",
                                       "customerId")},
            ensure_ascii=False, indent=1), name="B端订单")
    assert order.get("status") == STATUS_PAID, (
        f"0元电子票单在 B 端应为待核销({STATUS_PAID})，实际 {order.get('statusName')}"
    )
    assert order.get("deliveryType") == 4, "应为电子票(4)"
    assert order.get("statusName") == "待核销", "B 端状态名不符"


@allure.feature("核销链路")
@allure.story("B端核销")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("核销闭环：C端出票 → B端核销 → 订单变已完成 → 重复核销被拒")
@pytest.mark.skipif(not HAS_B_SESSION, reason=skip_b)
@pytest.mark.skipif(FREE_PRODUCT is None, reason=no_fixture)
def test_b_verify_full_chain(c_client, b_client):
    """核销主链路（会消耗一张票，所以下单+核销+重复核销放在同一条用例里）。

    断言口径跟随**运行时探测**（第②步的 has_counts = 票据接口有没有 totalVerifyCount）：
      - 有次数字段：核对 totalVerifyCount / usedVerifyCount / remainVerifyCount、留下核销记录
      - 没有次数字段（老版本接口）：退化为核对"票状态 1→2"+"订单 11→7"，
        业务语义等价，只是少了次数的细粒度观测

    为什么不用静态开关（env.supports_multi_verify）：
      该开关 2026-09-29 记为"生产不返回次数字段"，2026-09-30 复跑发现生产**已经返回**
      （票据带 totalVerifyCount/verifyRecordList、核销记录接口也有数据）→ 开关过期就变成假红。
      改用运行时探测后，接口升级/回滚都不需要人改配置。
      ⚠️ 生产仍缺的是**场次级核销次数配置**（B 端 events/edit 无 maxVerifyCount 字段），
         所以生产所有票的 totalVerifyCount 恒为 1，"多次核销"这条业务能力在生产仍不可覆盖。
    """
    with allure.step("① C 端 0 元下单并出票"):
        order_id, code = _make_free_order(c_client)
        detail = c_client.get_order_detail(order_id)
        allure.attach(f"orderId={order_id} 券码={code} "
                      f"状态={status_text(detail.get('status'))}", name="C端夹具")
    assert detail.get("status") == STATUS_PAID, "0 元单未直接落到待核销/待使用"
    assert code, "C 端未出票"

    with allure.step("② 等 B 端可见并读初始核销次数"):
        assert b_client.wait_order(order_id, timeout=40), f"B 端查不到订单 {order_id}"
        before = b_client.get_eticket(order_id)
        has_counts = "totalVerifyCount" in before          # 环境能力探测
        total, used = before.get("totalVerifyCount"), before.get("usedVerifyCount")
        if has_counts:
            allure.attach(f"核销次数 {used}/{total}，票状态 {before.get('status')}",
                          name="核销前")
            assert used == 0, "新票核销次数应为 0"
            assert total and total >= 1, "券码未配置核销次数上限"
        else:
            # 老版本接口：票据不返回核销次数字段
            allure.attach(
                f"环境 [{env.name}] 票据接口不返回核销次数字段（运行时探测），"
                f"改用票状态判断。票状态={before.get('status')}", name="核销前(降级模式)")
            assert before.get("status") == 1, (
                f"新票状态应为未核销(1)，实际 {before.get('status')}")

    with allure.step(f"③ B 端核销券码 {code}"):
        t0 = time.time()
        result = b_client.verify(code)
        allure.attach(f"{json.dumps(result, ensure_ascii=False)} "
                      f"(耗时 {round(time.time() - t0, 1)}s)", name="核销响应")

    with allure.step("④ 校验核销结果"):
        time.sleep(1)
        after = b_client.get_eticket(order_id)
        allure.attach(json.dumps(after, ensure_ascii=False, indent=1)[:800],
                      name="核销后票据")
    if has_counts:
        assert after.get("usedVerifyCount") == used + 1, "核销次数未+1"
        assert after.get("remainVerifyCount") == total - used - 1, "剩余次数不对"
        if after.get("remainVerifyCount") == 0:
            assert after.get("status") == 2, "核销完次数后电子票应为已核销(2)"
    else:
        assert after.get("status") == 2, (
            f"核销后电子票状态应为已核销(2)，实际 {after.get('status')}")

    with allure.step("⑤ 校验订单状态：次数用尽则变已完成(7)"):
        # 有次数字段时按 total 判断；降级模式按"单次核销"处理（生产 verificationNum 全为 1）
        single_verify = (total == 1) if has_counts else True
        deadline = time.time() + 20
        order = None
        while time.time() < deadline:
            order = b_client.get_order(order_id)
            if order and order.get("status") == STATUS_FINISHED:
                break
            time.sleep(2)
        allure.attach(json.dumps(
            {k: order.get(k) for k in ("status", "statusName", "statusText")},
            ensure_ascii=False), name="核销后订单")
    if single_verify:
        assert order and order.get("status") == STATUS_FINISHED, (
            f"单次核销票用完后订单应变已完成({STATUS_FINISHED})，"
            f"实际 {order and order.get('statusName')}"
        )
    else:
        assert order and order.get("status") == STATUS_PAID, "还有剩余次数时不应变已完成"

    with allure.step("⑥ 重复核销应被拒绝（上限校验）"):
        again_code, again_msg = b_client.verify_result(code)
        allure.attach(f"code={again_code} message={again_msg}", name="重复核销响应")
    assert again_code != 200, "重复核销居然成功了，上限校验失效"
    assert "核销" in (again_msg or ""), f"拒绝原因不明确：{again_msg}"

    with allure.step("⑦ 核销记录可查（留痕）"):
        records = b_client.verify_records(code)
        allure.attach(json.dumps(records, ensure_ascii=False, indent=1)[:600],
                      name="核销记录")
    # 口径同样跟随第②步的运行时探测：票据带核销次数字段 = 该批"核销留痕"特性已上线，
    # 记录接口就该有数据；不带 = 老版本，返回空是预期。
    if has_counts:
        assert records, "票据已带核销次数字段，但核销记录为空 —— 核销未留痕"
        allure.attach(
            f"票据接口带核销次数字段 → 记录接口同样应有数据，实测 {len(records)} 条。",
            name="留痕校验", attachment_type=allure.attachment_type.TEXT,
        )
    else:
        assert records == [], (
            f"票据接口不带核销次数字段，但核销记录接口返回了 {len(records)} 条 —— "
            "两次探测结论矛盾，接口版本可能不一致"
        )
        allure.attach(
            "票据接口无核销次数字段 → 记录接口返回空（符合预期）。\n"
            "留痕由以下三处佐证：核销响应=核销成功、票状态 1→2、订单 11→7、重复核销被拒",
            name="留痕校验（降级模式）", attachment_type=allure.attachment_type.TEXT,
        )


@allure.feature("核销链路")
@allure.story("B端核销")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("核销反例：不存在的券码被拒绝")
@pytest.mark.skipif(not HAS_B_SESSION, reason=skip_b)
def test_b_verify_invalid_code(b_client):
    """防呆：乱传券码必须报错，不能"核销成功"。"""
    with allure.step("用一个不存在的券码核销"):
        code, msg = b_client.verify_result("99999999999")
        allure.attach(f"code={code} message={msg}", name="响应")
    assert code != 200, "无效券码竟然核销成功"
    assert msg, "失败但没有原因"
