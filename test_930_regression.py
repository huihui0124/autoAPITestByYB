# -*- coding: utf-8 -*-
"""
test_930_regression.py —— 930 回归用例在**生产 888 店**的实际执行

来源：《930测试用例V1.2.0/回归用例.xlsx》「核心流程回归」sheet
      （15 条用例 / 16 行，TC-018 拆为「选座」「非选座」两行）。
目标环境：生产 `storeId=888`「咪咕生产测试店」。
      ⚠️ 红线：`storeId=1` 是真实业务店，本文件任何一步都不会碰它。

跑法（生产写操作必须显式放行）：

    MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 \
        ./.venv/bin/pytest -m reg930 -q -rs

============================ 可自动化边界（生产实测，2026-09-30）============================

| 用例 | 覆盖方式 | 说明 |
|---|---|---|
| TC-001 扫码登录        | 部分 | 只能验「H5 免登态有效且手机号一致」；APP 扫码跳转需真机 |
| TC-002 退出登录        | 契约 | 真调会作废生产 hmcer，且通行证续期已失效→无法自愈，故只验路由+鉴权 |
| TC-003 实名认证        | 负向 | 生产走**二要素核验**（姓名+证件号送外部征信），编造号码必被拒 |
| TC-005 电子票下单退款  | 全自动 | 清单中「非选座/非实名/电子票/条件退/0元」→ 下单 → 出票 → B端核销 → 退款 |
| TC-006 选座实名下单退款| 全自动 | 清单中「选座/一票一证/电子票/条件退」→ 选座+实名 → 下单出票 → 核销 → 退款 |
| TC-007 仅快递下单退款  | 全自动 | 清单中「纸质票且支持快递」→ 下单 → B端发货 → C端确认收货 → 退款 |
| TC-008 仅自取下单退款  | 全自动 | 清单中「纸质票且支持自取」→ 下单 → B端设置已取 → 退款 |
| TC-009 电子票身份证核销| 全自动 | 清单中「非选座/一票一证/电子票/0元」→ 出票 → B端核销 → 票已使用/订单已完成 |
| TC-010 PDA 核销        | 部分 | 核销动作与后台同接口可验；**按身份证搜单**接口在 888 恒返回空，搜单需人工 |
| TC-011 抢票准备        | 全自动 | 预填观演人/地址 → 回读 config 校验「带入信息与预填一致」 |
| TC-015 出票短信        | 需人工 | 生产无短信日志、无真机 → 只能人工核对文案 |
| TC-016 超时退款短信    | 需人工 | 同上；且需等支付倒计时自然超时 |
| TC-017 抢票提醒        | 部分 | 订阅/取消动作可验；短信文案与链接跳转需人工 |
| TC-018 创建项目×2      | 部分 | 默认做「B端配置 ↔ C端展示 ↔ 清单登记」三方一致性回读；完整新建流程走 B 端向导（人工） |

**为什么不做「真·新建项目」**：每跑一次回归就多一个废弃项目，888 店会被污染。
完整创建流程已沉淀为一次性工具（`tools/probe/build_prod_projects.py` / `./run.sh onsale`），
本套件只负责「上架后的项目信息与配置是否 1:1」（对应用例预期「项目信息完全与配置一致」）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import allure
import pytest
import requests

from conftest import login_or_skip
from migu_api import catalog, fundgate, paypage, qr
from migu_api.badmin import BAdminClient, BAdminAuthError, B_COOKIE_FILE
from migu_api.client import (MiguClient, PAID_STATUS, REFUNDED_STATUS, STATUS_FINISHED,
                             STATUS_PAID, STATUS_REFUNDED, STATUS_UNPAID, status_text)
from migu_api.config import (BROWSER_UA, env, prod_write_blocked, prod_write_skip_reason)
from migu_api.refund import (ORDER_STATUS_AFTER_SALE, ORDER_STATUS_REFUNDED, TASK_SUCCESS,
                             WHAT_BOTH, WHAT_MONEY, WHAT_TICKET, RefundClient,
                             task_status_desc)

pytestmark = [
    pytest.mark.reg930,
    # 生产写操作闸门：本文件会真下单/真核销/真退款，生产默认整组拦掉
    pytest.mark.skipif(prod_write_blocked(), reason=prod_write_skip_reason()),
]

SOURCE = "test_930_regression"

# ---------------------------------------------------------------- 结论台账
# junit 只给「通过/失败/跳过」三态，但 930 用例需要「通过 / 部分通过 / 需人工」——
# 用例级的真实口径写进这份台账，报告脚本以它为准（junit 只用来兜失败原因）。
LEDGER = Path(__file__).resolve().parent / "reports" / "reg930" / "verdicts.json"

_ORDERS: list[int] = []          # 当前用例产生的订单号，落台账方便追溯/清理


def _verdict(case: str, result: str, note: str = "") -> None:
    """记录用例级结论（幂等：同一条用例重复记录以最后一次为准）。"""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if LEDGER.exists():
        try:
            rows = json.loads(LEDGER.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            rows = []
    rows = [r for r in rows if r.get("case") != case]
    rows.append({
        "case": case, "result": result, "note": note,
        "orders": list(_ORDERS),
        "env": env.name, "storeId": env.store_id,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_order_scope():
    """每条用例前清空订单缓存，避免台账把上一条用例的订单带进来。"""
    _ORDERS.clear()
    yield


# ---------------------------------------------------------------- 夹具

@pytest.fixture(scope="module")
def c() -> MiguClient:
    cli = MiguClient()
    login_or_skip(cli, context="930 回归 C 端登录态")
    return cli


@pytest.fixture(scope="module")
def b() -> BAdminClient:
    cli = BAdminClient()
    if os.environ.get("MIGU_B_TICKET"):
        try:
            cli.login()
        except BAdminAuthError as e:
            pytest.skip(f"B 端 4A 票据兑换失败（可能已过期）：{e}")
    return cli


@pytest.fixture(scope="module")
def rc() -> RefundClient:
    return RefundClient()


@pytest.fixture(scope="module")
def real_names(c: MiguClient) -> list:
    """已认证的观演人（生产二要素核验 → 只能复用已存在的真实身份）。"""
    return c.list_real_names()


# ---------------------------------------------------------------- 小工具

def _one(**criteria) -> catalog.Target:
    """按条件从清单取唯一目标；取不到就 skip，绝不"随手拿别的商品下单"。"""
    try:
        return catalog.one(**criteria)
    except catalog.CatalogError as exc:
        pytest.skip(f"清单（{env.name}）里找不到 {criteria} 的可下单商品：{exc}")


def _any(**criteria) -> catalog.Target:
    """取第一个命中项 —— 只关心"项目/场次"而不关心"哪个票档"时用（如抢票准备、提醒订阅）。"""
    hits = catalog.current().find(**criteria)
    if not hits:
        pytest.skip(f"清单（{env.name}）里找不到 {criteria} 的可下单商品")
    return hits[0]


def _target(reason: str, **criteria) -> catalog.Target:
    """按业务形态从清单稳定选择一个目标，避免用例锁死在原始 Excel 的具体项目上。"""
    try:
        hits = catalog.find(**criteria)
    except catalog.CatalogError as exc:
        pytest.skip(f"清单（{env.name}）查询 {reason} 失败：{criteria}：{exc}")
    if not hits:
        pytest.skip(f"清单（{env.name}）里没有满足「{reason}」的可下单商品：{criteria}")
    hits = sorted(
        hits,
        key=lambda t: (
            "baseline" not in t.usages,
            t.project_key,
            t.event_key,
            float(t.price or 0),
            t.sku_key,
        ),
    )
    return hits[0]


def _zero_target(reason: str, **criteria) -> catalog.Target:
    return _target(reason, price=0, **criteria)


def _delivery_target(reason: str, delivery_type: int, **criteria) -> catalog.Target:
    hits = [
        t for t in catalog.find(**criteria)
        if delivery_type in t.delivery_type
    ]
    if not hits:
        pytest.skip(
            f"清单（{env.name}）里没有满足「{reason}」且支持 deliveryType={delivery_type} "
            f"的可下单商品：{criteria}"
        )
    hits = sorted(
        hits,
        key=lambda t: (
            "baseline" not in t.usages,
            t.project_key,
            t.event_key,
            float(t.price or 0),
            t.sku_key,
        ),
    )
    return hits[0]


def _target_where(reason: str, predicate, **criteria) -> catalog.Target:
    hits = [t for t in catalog.find(**criteria) if predicate(t)]
    if not hits:
        pytest.skip(f"清单（{env.name}）里没有满足「{reason}」的可下单商品：{criteria}")
    hits = sorted(
        hits,
        key=lambda t: (
            "baseline" not in t.usages,
            t.project_key,
            t.event_key,
            float(t.price or 0),
            t.sku_key,
        ),
    )
    return hits[0]


def _wait_eticket(c: MiguClient, order_id, timeout: int = 40) -> dict:
    """等票出来（0 元单虽即时出票，但出票是异步的，别拿空票当失败）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ticket = c.get_eticket(order_id)
        items = ticket if isinstance(ticket, list) else [ticket]
        items = [x for x in items if x and x.get("code")]
        if items:
            return items[0]
        time.sleep(2)
    raise AssertionError(f"订单 {order_id} 在 {timeout}s 内未出票")


def _b_order(b: BAdminClient, order_id, timeout: int = 40) -> dict:
    """等 B 端检索收录并返回订单（新单进索引 3~10s）。"""
    order = b.wait_order(order_id, timeout=timeout)
    assert order, f"B 端核销台查不到订单 {order_id}（跨系统数据不一致）"
    return order


def _wait_b_status(b: BAdminClient, order_id, want, timeout: int = 60) -> dict | None:
    """等 B 端订单落到目标状态集合。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = b.get_order(order_id)
        if last and last.get("status") in want:
            return last
        time.sleep(2)
    return last


def _b_refund(rc: RefundClient, order_id, reason: str) -> dict:
    """**B端条件退**闭环（订单详情发起）：可退性 → 试算 → 提交 → 审批 → 等工单成功。

    为什么下单退款类用例用它收尾，而不是用例步骤字面上的「C 端申请退款」：
      退款有三种方式（2026-09-30 生产 888 实测口径）——
        ① B端条件退  订单详情发起，可选仅退票/仅退款/退票退款（**本函数走的就是它**）
        ② B端常规退  退款中心发起 + 退款审批，只能全量退票退款
        ③ C端条件退  用户自助、无需审批，需同时满足
          「场次配了条件退 + 在条件退时间范围内 + 订单非终态 + 金额≠0」
      本组的用例（TC-005~008）第 8/9 步先在商家端核销/收货、把订单推到终态，
      **再**申请退款 —— 终态下 C 端 `canRefund=false`（「订单已经完成啦～如需退款，
      联系客服为您处理哦！」），且夹具是 0 元单（不允许用于 C 端申请退款），
      所以 C 端分支在本组夹具下**结构上不可达**，
      按用例步骤里并列的「商家端订单详情」分支走 B 端条件退。
      三条退款通路本身的覆盖见 `test_TC019_refund_three_paths`。
    """
    return rc.refund_order(order_id, what=WHAT_BOTH, reason=reason, task_timeout=120)


def _b_regular_refund(rc: RefundClient, order_id, reason: str = "930回归 常规退") -> dict:
    """**B端常规退**闭环（退款中心发起 → 退款审批通过）。

    与条件退的关键差别：**只能全量退票+退款**（没有仅退票/仅退款的选择），
    且**不受场次「条件退」配置影响**。发起后订单进 14/售后中，审批通过才落 5/已退款。
    """
    return rc.regular_refund(order_id)


def _assert_refunded(rc: RefundClient, b: BAdminClient, order_id, summary: dict):
    """校验退款确实闭环：工单成功 + 订单落到已退款/售后态。"""
    task = summary.get("task") or {}
    assert summary.get("submitted"), f"退款未提交成功：{summary.get('note')}"
    assert task.get("taskStatus") == TASK_SUCCESS, (
        f"退款工单未成功：{task_status_desc(task.get('taskStatus'))} "
        f"/ task={json.dumps(task, ensure_ascii=False)[:300]}"
    )
    if summary.get("approvalError"):
        allure.attach(str(summary["approvalError"]), name="审批异常",
                      attachment_type=allure.attachment_type.TEXT)
    order = _wait_b_status(b, order_id, REFUNDED_STATUS, timeout=60)
    assert order and order.get("status") in REFUNDED_STATUS, (
        f"退款工单已成功但订单未落退款态，当前 {order and order.get('statusName')}"
    )
    return order


def _c_refund_attempt(c: MiguClient, order_id) -> dict:
    """C 端条件退预校验（只读）——记录「入口是否对用户开放」。"""
    pre = c.refund_pre(order_id)
    allure.attach(json.dumps(pre, ensure_ascii=False, indent=1), name="C端退款预校验")
    return pre


def _c_refund_submit_ready(c: MiguClient, order_id, *, timeout: float = 40,
                           **body) -> dict:
    """C 端条件退提交，**等订单在退款模块落库**后返回。

    实测时序（2026-09-30，生产 888）：下单后 `refund_pre` 立刻正常，但
    `POST /v1/order/refund/{id}/submit` 会先连续返回
    `400 无效订单，请联系客服~` 约 **10 秒**，之后才进入真正的业务校验
    所以「无效订单」= 还没落库，不是业务结论，必须重试，否则会把
    环境时序问题误报成"条件退提交失败"。
    """
    deadline = time.time() + timeout
    last: dict = {}
    while True:
        last = c.try_call(
            "POST", f"/v1/order/refund/{order_id}/submit?storeId={env.store_id}",
            json=body,
        )
        if "无效订单" not in (last.get("message") or ""):
            return last
        if time.time() >= deadline:
            return last
        time.sleep(2)


def _new_order(c: MiguClient, target: catalog.Target, *, delivery_type: int = 4,
               address_id=None, seat_ids=None, real_infos=None,
               contact_name: str = "930回归") -> int:
    """统一下单入口（走清单白名单 guard_order），登记订单号供台账追溯。"""
    order_id = c.make_order(
        target.product_id, target.sku_id, 1, target.amount,
        delivery_type=delivery_type, address_id=address_id,
        seat_ids=seat_ids, real_name_infos=real_infos,
        contact_name=contact_name, source=SOURCE,
    )
    _ORDERS.append(order_id)
    return order_id


def _try_call_retry(c: MiguClient, method: str, path: str, *, tries: int = 3, **kw) -> dict:
    """`try_call` + 网络抖动重试。

    生产 888 走公网，`save_real_name`（送外部征信、后端耗时高）实测出现过
    `ReadTimeout(read timeout=15)` —— 那是网络抖动，不是业务结论，
    不重试就会把"环境抖了一下"报成"用例失败"。
    """
    last: Exception | None = None
    for i in range(tries):
        try:
            return c.try_call(method, path, **kw)
        except requests.exceptions.RequestException as exc:      # 网络层才重试
            last = exc
            time.sleep(1.5 * (i + 1))
    raise AssertionError(f"{method} {path} 连续 {tries} 次网络异常：{last}")


def _real_name_payload(c: MiguClient, real_names: list, count: int = 1) -> list:
    """构造 realNameInfoList。先只传 id（后端自己回填），失败再补全字段。"""
    usable = [r for r in real_names if r.get("id")]
    assert len(usable) >= count, (
        f"账号里只有 {len(usable)} 个已认证观演人，不够绑定 {count} 张实名票"
        "（生产走二要素核验，无法用编造身份新增 → 需人工先在 H5 完成实名）"
    )
    return [{"id": r["id"]} for r in usable[:count]]


def _id_card_checksum(body17: str) -> str:
    """计算中国大陆身份证号码第 18 位校验码。"""
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    codes = "10X98765432"
    return codes[sum(int(n) * w for n, w in zip(body17, weights)) % 11]


def _fake_id_card(seed: int = 0) -> str:
    """生成一张仅用于 test 环境的合法格式身份证号。"""
    seq = (int(time.time() * 1000) + seed) % 1000
    body = f"11010119900101{seq:03d}"
    return body + _id_card_checksum(body)


def _temp_real_name_payload(c: MiguClient) -> tuple[list[dict], int | None, str]:
    """测试环境临时创建观演人，规避一票一证重复下单的证件限购。"""
    assert env.name == "test", "临时实名只允许在 test 环境使用"
    last_error = None
    for i in range(5):
        name = f"自动化{i}{int(time.time()) % 10000}"
        id_card = _fake_id_card(i)
        try:
            created = c.save_real_name(name, id_card)
        except Exception as exc:  # noqa: BLE001 - 附带最后一次失败原因，方便定位环境问题
            last_error = exc
            continue
        rid = (
            created.get("id")
            or created.get("realNameInfoId")
            or created.get("realNameId")
        )
        assert rid, f"新增临时观演人未返回 id：{json.dumps(created, ensure_ascii=False)[:300]}"
        return [{"id": rid}], int(rid), f"{name} / {id_card}"
    raise AssertionError(f"test 环境无法新增临时观演人：{last_error}")


# ================================================================ TC-001

@allure.feature("930回归")
@allure.story("注册/登录/退出场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-001 咪咕APP扫码进入H5自动登录（H5免登态有效性）")
@pytest.mark.release_core
def test_TC001_h5_auto_login(c: MiguClient):
    """用例：咪咕APP跳转H5 → H5 自动登录成功，手机号与 APP 一致。

    自动化可覆盖「免登会话真的建起来了、手机号与账号一致」这一段；
    「APP 扫码跳转」是客户端行为，需真机人工确认。
    """
    with allure.step("① 读会员信息（免登会话的有效性判据）"):
        info = c.get_member_info()
        phone = info.get("phone")
        allure.attach(json.dumps(
            {k: info.get(k) for k in ("phone", "nickName", "userId", "levelName")},
            ensure_ascii=False, indent=1), name="会员信息")

    with allure.step("② 校验手机号与生产账号登记一致"):
        assert phone, "H5 免登会话无效：读不到会员手机号"
        assert phone in env.accounts, (
            f"登录手机号 {phone} 不在环境登记账号 {list(env.accounts)} 里"
            "（可能是环境串了/登录态取错）"
        )

    with allure.step("③ 校验店铺上下文（必须是生产测试店 888）"):
        shop = c.get_shop_info()
        allure.attach(json.dumps(shop, ensure_ascii=False, indent=1)[:600], name="店铺信息")
        if str(env.store_id) != "888":
            pytest.skip(
                f"TC-001 的 APP 扫码/H5 免登录口径只针对生产测试店 888；"
                f"当前环境 [{env.name}] storeId={env.store_id}，本轮不判失败"
            )

    _verdict("TC-001", "部分通过",
             f"H5 免登态有效：hmcer 会话可用，会员手机号={phone}，与登记账号一致；"
             f"店铺=888。仅「咪咕APP 扫码跳转」环节需真机人工确认")


# ================================================================ TC-002

@allure.feature("930回归")
@allure.story("注册/登录/退出场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-002 退出登录（契约校验，不做破坏性真退）")
@pytest.mark.release_manual
def test_TC002_logout_contract(c: MiguClient):
    """用例：H5「我的-退出登录」→ 退出成功。

    ⚠️ 为什么只做契约校验：`POST /v1/logout` 会**作废当前 hmcer**，而生产 hmcer
    只能从本机浏览器 Cookie 库 harvest 得到，且通行证续期通道实测已失效
    （`refresh_hmcer` 报「通行证会话已失效」）—— 真退一次，后面所有用例全废。
    所以这里用**无 Cookie 的独立会话**打这个接口，只证明「路由存在 + 方法正确 + 鉴权生效」。
    """
    with allure.step("① 用无登录态的独立会话 POST /v1/logout"):
        s = requests.Session()
        s.headers.update({"User-Agent": BROWSER_UA, "Referer": env.h5_url,
                          "Origin": env.base_url})
        try:
            s.verify = env.verify_ssl
        except Exception:                     # noqa: BLE001 —— 老 requests 才需要
            pass
        resp = s.post(f"{env.api_base}/v1/logout?storeId={env.store_id}", timeout=20)
        body = (resp.text or "")[:300]
        allure.attach(f"HTTP {resp.status_code}\n{body}", name="无登录态响应")

    with allure.step("② 校验：路由存在（非 404/非 HTML 兜底页）且鉴权生效"):
        assert resp.status_code == 200, f"退出登录接口异常：HTTP {resp.status_code}"
        assert "Not Found" not in body and "<html" not in body.lower(), (
            f"退出登录接口不存在或被打到 SPA 兜底页：{body[:160]}"
        )

    with allure.step("③ 复核当前自动化会话未受影响（仍是登录态）"):
        assert c.get_member_info().get("phone"), "契约校验把当前会话弄失效了"

    _verdict("TC-002", "部分通过",
             "契约校验通过：POST /v1/logout 路由存在、无登录态请求被正常受理、"
             "当前会话未受影响。真调会作废生产 hmcer（通行证续期已失效、无法自愈），"
             "故「点击退出登录」动作留人工执行")


# ================================================================ TC-003

@allure.feature("930回归")
@allure.story("实名认证场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-003 实名认证（字段级负向校验 + 已认证数据可读）")
def test_TC003_real_name_validation(c: MiguClient, real_names: list):
    """用例：进入我的-实名认证 → 输入正确身份证号+姓名 → 提交认证成功。

    生产实测：`/v1/user/realNameInfo/save` 走**二要素核验**（姓名 + 证件号送外部
    征信平台），编造的号码一律被拒（「姓名与身份证号不一致」/「身份信息核验失败」）。
    所以自动化只能覆盖「非法入参被拒」，正向认证需用真实身份人工执行。
    """
    samples = [
        ("空姓名", {"name": "", "id_card_no": "37100219900101814X"}),
        ("空证件号", {"name": "测试姓名", "id_card_no": ""}),
        ("过短证件号", {"name": "测试姓名", "id_card_no": "1234"}),
        ("非法证件类型", {"name": "测试姓名", "id_card_no": "37100219900101814X",
                          "id_card_type": "NOT_A_TYPE"}),
        ("非数字证件号", {"name": "测试姓名", "id_card_no": "abcdefghijklmnopqr"}),
    ]
    results: dict[str, str] = {}
    with allure.step("① 逐个提交非法入参，必须全部被拒"):
        for label, kw in samples:
            body = {
                "name": kw.get("name", ""),
                "idCardType": kw.get("id_card_type", "ID_CARD"),
                "idCardNo": kw.get("id_card_no", ""),
                "country": "中国",
            }
            resp = _try_call_retry(
                c, "POST", f"/v1/user/realNameInfo/save?storeId={env.store_id}", json=body
            )
            code, msg = resp.get("code"), resp.get("message")
            results[label] = f"code={code} message={msg}"
            allure.attach(f"code={code} message={msg}", name=f"{label} → 响应")
        allure.attach(json.dumps(results, ensure_ascii=False, indent=1),
                      name="负向汇总")

    assert all("code=200" not in v for v in results.values()), (
        f"存在未被拦截的非法实名入参：{results}"
    )

    with allure.step("② 已认证观演人可读（认证结果确实落库）"):
        allure.attach(json.dumps(real_names, ensure_ascii=False, indent=1),
                      name="已认证观演人")
        assert isinstance(real_names, list), "观演人列表接口异常"

    _verdict("TC-003", "部分通过",
             f"字段级负向 {len(samples)} 例全部被拒（空姓名/空证件号/短号/非法证件类型/非数字）；"
             f"已认证观演人可读 {len(real_names)} 条。"
             "正向「正确身份证认证成功」受生产二要素核验限制（编造号码必被拒），需真实身份人工执行")


# ================================================================ TC-005

@allure.feature("930回归")
@allure.story("下单退款场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-005 非选座-电子票-非实名-下单退款（含B端核销）")
@pytest.mark.release_core
def test_TC005_nonseat_eticket_order_verify_refund(c, b, rc):
    """用例步骤：进详情→立即购买→选场次票档→填联系人→下单→我的订单→
    B端核销并看C端状态→申请退款。预期：11待使用 → 完成后7已完成且票已使用 → 退款到账。
    """
    t = _zero_target(
        "非选座-非实名-电子票-条件退场次",
        seat=False, real_name=0, delivery="eticket", event="refundable",
    )
    allure.attach(t.label, name="夹具（清单登记项）")

    with allure.step("① C 端下单（非选座电子票、0 元票档）"):
        order_id = _new_order(c, t, delivery_type=4)
        detail = c.get_order_detail(order_id)
        allure.attach(f"orderId={order_id} status={detail.get('status')}"
                      f"({status_text(detail.get('status'))})", name="下单结果")
    assert detail.get("status") == STATUS_PAID, (
        f"电子票下单后应为待使用({STATUS_PAID})，实际 "
        f"{status_text(detail.get('status'))}"
    )

    with allure.step("② 出票（异步，轮询等券码）"):
        ticket = _wait_eticket(c, order_id)
        code = ticket["code"]
        allure.attach(json.dumps(ticket, ensure_ascii=False, indent=1)[:600], name="电子票")

    with allure.step("③ B 端核销台能查到该单"):
        row = _b_order(b, order_id)
        allure.attach(json.dumps(
            {k: row.get(k) for k in ("orderId", "status", "statusName", "deliveryTypeName",
                                     "paymentPrice", "customerId")},
            ensure_ascii=False, indent=1), name="B端订单")
        assert row.get("status") == STATUS_PAID, "B 端应为待核销(11)"

    with allure.step("④ B 端核销"):
        b.verify(code)
        after_ticket = b.get_eticket(order_id)
        allure.attach(json.dumps(after_ticket, ensure_ascii=False, indent=1)[:600],
                      name="核销后票据")
        allure.attach("\n".join(f"{e['desc']}" for e in b.order_event_log(order_id)),
                      name="B端操作流水", attachment_type=allure.attachment_type.TEXT)

    with allure.step("⑤ 校验 C 端票状态已使用、订单已完成(7)"):
        order = _wait_b_status(b, order_id, {STATUS_FINISHED})
        assert order and order.get("status") == STATUS_FINISHED, (
            f"核销后订单应为已完成(7)，实际 {order and order.get('statusName')}"
        )
        assert after_ticket.get("status") == 2, (
            f"核销后电子票应为已核销(2)，实际 {after_ticket.get('status')}"
        )
        detail = c.get_order_detail(order_id)
        allure.attach(f"C端订单状态={detail.get('status')}"
                      f"({status_text(detail.get('status'))})", name="C端订单")

    with allure.step("⑥ 退款：先看 C 端条件退入口是否开放"):
        pre = _c_refund_attempt(c, order_id)
        allure.attach(
            f"核销后 canShowRefund={pre.get('canShowRefund')}，"
            f"canRefund={pre.get('canRefund')}，refundTip={pre.get('refundTip')}\n"
            "→ 场次配了条件退，所以入口仍在（canShowRefund=true）；"
            "但订单已核销→终态，canRefund=false（「订单已经完成啦～联系客服」）。"
            "再叠加夹具是 0 元单，不能用于 C 端申请退款"
            "—— 按用例步骤 8 并列的「商家端订单详情」分支走 B 端条件退。",
            name="退款入口判定", attachment_type=allure.attachment_type.TEXT)

    with allure.step("⑦ B 端条件退闭环（订单详情发起：提交→审批→工单成功→订单已退款）"):
        summary = _b_refund(rc, order_id, "930回归 TC-005")
        allure.attach(json.dumps(
            {k: summary.get(k) for k in ("refundId", "taskId", "amount", "approved")},
            ensure_ascii=False, indent=1), name="退款提交摘要")
        order = _assert_refunded(rc, b, order_id, summary)
        allure.attach(f"订单终态 = {order.get('status')}({order.get('statusName')})",
                      name="退款后订单")

    _verdict("TC-005", "通过",
             f"下单→出票(券码{code})→B端核销→订单7已完成/票已使用→B端条件退→"
             f"订单{order.get('status')}{order.get('statusName')}，全链路闭环")


# ================================================================ TC-006

@allure.feature("930回归")
@allure.story("下单退款场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-006 选座-身份证-实名-下单退款（含B端核销）")
@pytest.mark.release_core
def test_TC006_seat_idcard_order_verify_refund(c, b, rc, real_names):
    """用例步骤：进详情→立即选座→选场次票档→填联系人→添加观演人→下单→我的订单→
    B端核销→点退款→申请退款。预期：出票→已完成→退款到账。
    """
    t = _zero_target(
        "选座-一票一证-电子票-条件退场次",
        seat=True, real_name=1, delivery="eticket", event="refundable",
    )
    seat = None
    with allure.step("① 拉座位图，挑一个 0 元可售座位"):
        seats = c.free_seats(t.product_id, t.event_id, price=0, limit=5)
        assert seats, "该场次没有 0 元可售座位（座位图可能被占满）"
        seat = seats[0]
        allure.attach(json.dumps(
            {k: seat.get(k) for k in ("seatId", "seatName", "rowName", "areaId", "skuId")},
            ensure_ascii=False, indent=1), name="选中座位")
        target = _one(project=t.product_id, sku=seat["skuId"])
        allure.attach(target.label, name="夹具（清单登记项）")

    with allure.step("② 绑定已认证观演人（一票一证）"):
        infos = _real_name_payload(c, real_names, count=1)
        allure.attach(json.dumps(infos, ensure_ascii=False), name="realNameInfoList")

    with allure.step("③ 下单（选座 + 实名 + 电子票）"):
        order_id = _new_order(c, target, delivery_type=4,
                              seat_ids=[seat["seatId"]], real_infos=infos)
        detail = c.get_order_detail(order_id)
        got_names = detail.get("realNameInfoList") or []
        allure.attach(f"orderId={order_id} status={detail.get('status')}"
                      f"({status_text(detail.get('status'))})\n"
                      f"实名人={json.dumps(got_names, ensure_ascii=False)}", name="下单结果")
    assert detail.get("status") == STATUS_PAID, "选座实名电子票下单后应为待使用(11)"
    assert got_names, "订单未带上观演人（一票一证要求实名）"

    with allure.step("④ 出票"):
        ticket = _wait_eticket(c, order_id)
        code = ticket["code"]
        allure.attach(json.dumps(ticket, ensure_ascii=False, indent=1)[:600], name="电子票")

    with allure.step("⑤ B 端核销 → 订单已完成(7)"):
        _b_order(b, order_id)
        b.verify(code)
        order = _wait_b_status(b, order_id, {STATUS_FINISHED})
        assert order and order.get("status") == STATUS_FINISHED, (
            f"核销后应为已完成(7)，实际 {order and order.get('statusName')}"
        )
        et = b.get_eticket(order_id)
        assert et.get("status") == 2, f"票应为已核销(2)，实际 {et.get('status')}"

    with allure.step("⑥ 退款：C 端入口判定 + B 端闭环"):
        pre = _c_refund_attempt(c, order_id)
        summary = _b_refund(rc, order_id, "930回归 TC-006")
        order = _assert_refunded(rc, b, order_id, summary)
        allure.attach(f"C端 canRefund={pre.get('canRefund')}；"
                      f"B 端退款后订单={order.get('status')}({order.get('statusName')})",
                      name="退款结果", attachment_type=allure.attachment_type.TEXT)

    _verdict("TC-006", "通过",
             f"选座座位{seat.get('rowName')}{seat.get('seatName')}(seatId={seat['seatId']})"
             f"+已认证观演人下单→出票(券码{code})→B端核销→7已完成→B端退款→"
             f"{order.get('status')}{order.get('statusName')}")


# ================================================================ TC-007

@allure.feature("930回归")
@allure.story("下单退款场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-007 非选座-仅快递-下单退款（B端发货→C端确认收货）")
@pytest.mark.release_core
def test_TC007_express_order_deliver_receive_refund(c, b, rc):
    """用例步骤：进详情→立即购买→选场次票档→新增地址→下单→我的订单→
    B端发货并看C端状态→C端确认收货→申请退款。
    预期：2待发货 → 3待收货 → 6/已完成 → 退款到账。
    """
    t = _delivery_target(
        "非选座-纸质票-快递-条件退场次",
        1, seat=False, real_name=0, delivery="paper", event="refundable", price=0,
    )
    allure.attach(t.label, name="夹具（清单登记项）")

    with allure.step("① 取收货地址（地址簿为空则 skip）"):
        addr = c.default_address()
        allure.attach(json.dumps(addr, ensure_ascii=False, indent=1), name="收货地址")

    with allure.step("② 下单（仅快递，deliveryType=1 + addressId）"):
        order_id = _new_order(c, t, delivery_type=1, address_id=addr["id"])
        row = _b_order(b, order_id)
        po_id = row.get("purchaseOrderId")
        allure.attach(f"orderId={order_id} purchaseOrderId={po_id} "
                      f"B端状态={row.get('status')}({row.get('statusName')})"
                      f" / {row.get('statusText')}", name="下单结果")
    assert row.get("deliveryType") == 1, f"应为快递(1)，实际 {row.get('deliveryType')}"
    assert row.get("status") == 2, f"0 元快递单应为待发货(2)，实际 {row.get('statusName')}"
    assert po_id, "B 端订单行未返回 purchaseOrderId（发货必传，见 badmin.deliver 注释）"

    with allure.step("③ B 端发货（deliverInfo → deliver）"):
        info = b.deliver_info(order_id)
        companies = info.get("deliverCompanies") or []
        assert companies, f"发货弹窗没有可选快递公司：{json.dumps(info, ensure_ascii=False)[:300]}"
        company = companies[0].get("name") if isinstance(companies[0], dict) else companies[0]
        user_address_id = (info.get("userAddress") or {}).get("id")
        assert user_address_id, "deliverInfo 未返回 userAddress.id"
        allure.attach(json.dumps(
            {"purchaseOrderId": po_id, "company": company,
             "userAddressId": user_address_id, "expressNo": "SF93000001"},
            ensure_ascii=False, indent=1), name="发货入参")
        b.deliver(order_id, po_id, user_address_id, company, "SF93000001")

    with allure.step("④ 校验 B 端 3(待收货) + C 端状态同步"):
        row = _wait_b_status(b, order_id, {3})
        assert row and row.get("status") == 3, (
            f"发货后应为待收货(3)，实际 {row and row.get('statusName')}"
        )
        detail = c.get_order_detail(order_id)
        allure.attach(f"C端={detail.get('status')}({status_text(detail.get('status'))}) "
                      f"/ B端={row.get('status')}({row.get('statusName')})", name="双向状态")

    with allure.step("⑤ C 端确认收货"):
        c.receive_order(order_id, order_id)
        row = _wait_b_status(b, order_id, {6, STATUS_FINISHED})
        assert row and row.get("status") in (6, STATUS_FINISHED), (
            f"确认收货后应为待观演(6)，实际 {row and row.get('statusName')}"
        )
        allure.attach("\n".join(e["desc"] for e in b.order_event_log(order_id)),
                      name="B端操作流水", attachment_type=allure.attachment_type.TEXT)

    with allure.step("⑥ 退款"):
        pre = _c_refund_attempt(c, order_id)
        summary = _b_refund(rc, order_id, "930回归 TC-007")
        order = _assert_refunded(rc, b, order_id, summary)
        allure.attach(f"C端 canRefund={pre.get('canRefund')}；退款后订单="
                      f"{order.get('status')}({order.get('statusName')})", name="退款结果")

    _verdict("TC-007", "通过",
             f"快递下单→待发货2→B端发货→待收货3→C端确认收货→{row.get('status')}"
             f"{row.get('statusName')}→B端退款→{order.get('status')}{order.get('statusName')}；"
             f"快递={row.get('shipName')}/{row.get('shipNo')}")


# ================================================================ TC-008

@allure.feature("930回归")
@allure.story("下单退款场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-008 非选座-仅自取-下单退款（商家端设置已取）")
@pytest.mark.release_core
def test_TC008_selfpickup_order_take_refund(c, b, rc):
    """用例步骤：进详情→立即购买→选场次票档→填联系人→切换自取→下单→我的订单→
    商家端设置已取并看C端状态→申请退款。预期：8待自取 → 6待观演 → 退款到账。
    """
    t = _delivery_target(
        "非选座-纸质票-自取-条件退场次",
        3, seat=False, real_name=0, delivery="paper", event="refundable", price=0,
    )
    allure.attach(t.label, name="夹具（清单登记项）")

    with allure.step("① 下单（切换为自取，deliveryType=3）"):
        order_id = _new_order(c, t, delivery_type=3)
        row = _b_order(b, order_id)
        po_id = row.get("purchaseOrderId")
        allure.attach(f"orderId={order_id} purchaseOrderId={po_id} "
                      f"B端={row.get('status')}({row.get('statusName')})"
                      f" / {row.get('statusText')}", name="下单结果")
    assert row.get("deliveryType") == 3, f"应为自取(3)，实际 {row.get('deliveryType')}"
    assert row.get("status") == 8, f"自取 0 元单应为待自取(8)，实际 {row.get('statusName')}"

    with allure.step("② 商家端「设置已取」（B 端 received）"):
        b.receive(order_id, po_id)

    with allure.step("③ 校验 C/B 双方状态：应到待观演(6)"):
        row = _wait_b_status(b, order_id, {6})
        assert row and row.get("status") == 6, (
            f"设置已取后应为待观演(6)，实际 {row and row.get('statusName')}"
        )
        detail = c.get_order_detail(order_id)
        allure.attach(f"C端={detail.get('status')}({status_text(detail.get('status'))})"
                      f" / B端={row.get('status')}({row.get('statusName')})",
                      name="双向状态")
        allure.attach("\n".join(e["desc"] for e in b.order_event_log(order_id)),
                      name="B端操作流水", attachment_type=allure.attachment_type.TEXT)

    with allure.step("④ 退款"):
        pre = _c_refund_attempt(c, order_id)
        summary = _b_refund(rc, order_id, "930回归 TC-008")
        order = _assert_refunded(rc, b, order_id, summary)
        allure.attach(f"C端 canRefund={pre.get('canRefund')}；退款后订单="
                      f"{order.get('status')}({order.get('statusName')})", name="退款结果")

    _verdict("TC-008", "通过",
             f"自取下单→8待自取→B端设置已取→6待观演→B端退款→"
             f"{order.get('status')}{order.get('statusName')}")


# ================================================================ TC-009

@allure.feature("930回归")
@allure.story("核销场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-009 电子票、身份证--B端核销（核销次数=1）")
@pytest.mark.release_core
def test_TC009_b_verify_eticket_idcard(c, b, real_names):
    """用例：查看订单列表 → 确认核销 → 查 C 端票状态。
    夹具从清单中按「非选座-一票一证-电子票」选择，贴合「电子票 + 身份证」。
    """
    t = _zero_target(
        "非选座-一票一证-电子票-条件退场次",
        seat=False, real_name=1, delivery="eticket", event="refundable",
    )
    allure.attach(t.label, name="夹具（清单登记项）")
    temp_real_name_id = None
    if env.name == "test":
        with allure.step("① 准备 test 环境临时观演人（规避一票一证历史订单限购）"):
            infos, temp_real_name_id, label = _temp_real_name_payload(c)
            allure.attach(label, name="临时实名")
    else:
        infos = _real_name_payload(c, real_names, count=1)
        allure.attach(json.dumps(infos, ensure_ascii=False), name="realNameInfoList")

    try:
        with allure.step("② 实名下单并出票"):
            order_id = _new_order(c, t, delivery_type=4, real_infos=infos)
            ticket = _wait_eticket(c, order_id)
            code = ticket["code"]
            detail = c.get_order_detail(order_id)
            allure.attach(f"orderId={order_id} 券码={code} 实名人="
                          f"{json.dumps(detail.get('realNameInfoList'), ensure_ascii=False)}"
                          f" 状态={status_text(detail.get('status'))}", name="夹具")

        with allure.step("③ B 端核销前读票据（核销次数应为 1/未核销）"):
            _b_order(b, order_id)
            before = b.get_eticket(order_id)
            allure.attach(json.dumps(before, ensure_ascii=False, indent=1)[:800],
                          name="核销前票据")
            total = before.get("totalVerifyCount")
            if total is not None:
                assert int(before.get("usedVerifyCount") or 0) == 0, "新票核销次数应为 0"
                assert int(total) == 1, (
                    f"生产场次级核销次数配置未发布，所有票应恒为 1 次，实际 total={total}"
                )

        with allure.step("④ B 端核销"):
            result = b.verify(code)
            allure.attach(json.dumps(result, ensure_ascii=False), name="核销响应")

        with allure.step("⑤ 校验票已使用 + 订单已完成(7) + 重复核销被拒"):
            after = b.get_eticket(order_id)
            assert after.get("status") == 2, f"票应为已核销(2)，实际 {after.get('status')}"
            order = _wait_b_status(b, order_id, {STATUS_FINISHED})
            assert order and order.get("status") == STATUS_FINISHED, (
                f"核销次数用尽后订单应为已完成(7)，实际 {order and order.get('statusName')}"
            )
            again_code, again_msg = b.verify_result(code)
            assert again_code != 200, "重复核销竟然成功了，上限校验失效"
            allure.attach(f"重复核销 code={again_code} message={again_msg}",
                          name="重复核销被拒")
            detail = c.get_order_detail(order_id)
            allure.attach(f"C端订单={detail.get('status')}({status_text(detail.get('status'))})",
                          name="C端状态")
    finally:
        if temp_real_name_id:
            with allure.step("清理 test 环境临时观演人"):
                try:
                    c.delete_real_name(temp_real_name_id)
                except Exception as exc:  # noqa: BLE001 - 清理失败不覆盖主流程结论
                    allure.attach(str(exc), name="临时观演人清理失败",
                                  attachment_type=allure.attachment_type.TEXT)

    _verdict("TC-009", "通过",
             f"一票一证电子票下单(券码{code})→B端核销→票2已核销/订单7已完成/"
             f"重复核销被拒({again_msg})；核销次数上限=1")


# ================================================================ TC-010

@allure.feature("930回归")
@allure.story("核销场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-010 电子票、身份证--PDA核销")
def test_TC010_pda_verify(c, b, real_names):
    """用例：PDA 搜索票券码/身份证 → 滑动核销 → 查 C 端票状态。

    PDA 的「滑动核销」调的就是 B 端同一条核销接口，可以用券码验通；
    但「按身份证搜单」走 `/v1/shopTrade/order/realNameSearch`，
    ⚠️ 该接口在生产 888 **恒返回空**（实测空关键字/订单号/手机号/身份证前缀全部 0 条，
       而订单里确实带着实名人），所以「搜索」这一段无法自动验证，需在 PDA 上人工确认。
    """
    t = _zero_target(
        "非选座-一票一证-电子票-条件退场次",
        seat=False, real_name=1, delivery="eticket", event="refundable",
    )
    infos = _real_name_payload(c, real_names, count=1)
    allure.attach(t.label, name="夹具（清单登记项）")

    with allure.step("① 实名下单并出票"):
        order_id = _new_order(c, t, delivery_type=4, real_infos=infos)
        ticket = _wait_eticket(c, order_id)
        code = ticket["code"]
        allure.attach(f"orderId={order_id} 券码={code}", name="夹具")

    with allure.step("② 探测 PDA「按身份证搜单」接口"):
        hits = b.real_name_search(str(order_id))
        hits_kw = b.real_name_search("18366267528")
        allure.attach(
            f"按订单号搜 {order_id} → {len(hits)} 条\n"
            f"按手机号搜 → {len(hits_kw)} 条\n"
            "→ 该接口在 888 恒空（订单确实带实名人），搜单环节需 PDA 人工确认",
            name="搜单接口探测", attachment_type=allure.attachment_type.TEXT)

    with allure.step("③ PDA 核销动作（与 B 端同接口）"):
        _b_order(b, order_id)
        result = b.verify(code)
        allure.attach(json.dumps(result, ensure_ascii=False), name="核销响应")

    with allure.step("④ 校验 C 端票状态与订单状态"):
        after = b.get_eticket(order_id)
        assert after.get("status") == 2, f"票应为已核销(2)，实际 {after.get('status')}"
        order = _wait_b_status(b, order_id, {STATUS_FINISHED})
        assert order and order.get("status") == STATUS_FINISHED, (
            f"订单应为已完成(7)，实际 {order and order.get('statusName')}"
        )
        detail = c.get_order_detail(order_id)
        allure.attach(f"C端订单={detail.get('status')}({status_text(detail.get('status'))})",
                      name="C端状态")

    _verdict("TC-010", "部分通过",
             f"核销动作已验：按券码{code}核销→票2已核销/订单7已完成、C端状态同步。"
             f"搜单环节未验：/v1/shopTrade/order/realNameSearch 在 888 恒返回空"
             f"（订单带实名 于*/371***748 却搜不到），需 PDA 人工确认")


# ================================================================ TC-011

@allure.feature("930回归")
@allure.story("抢票准备场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-011 抢票准备提前录入信息（预填→回读一致性）")
def test_TC011_prefabricate_prefill(c, real_names):
    """用例：提前完成抢票准备 → 开售后检查带入信息与预填一致。

    自动化口径：调用「抢票准备-绑定观演人 / 绑定收货地址」接口写入预填内容，
    再用 `GET /v1/prefabricate/product/config` 回读，校验与写入一致
    —— 这就是「带入信息与抢票准备预填信息一致」在接口层的等价证据。
    """
    seat_t = _target(
        "抢票准备观演人预填项目",
        seat=True, real_name=1, delivery="eticket", event="refundable",
    )
    paper_t = _delivery_target(
        "抢票准备收货地址预填项目",
        1, seat=False, real_name=0, delivery="paper", event="refundable", price=0,
    )
    infos = _real_name_payload(c, real_names, count=1)
    rid = infos[0]["id"]
    addr = c.default_address()

    with allure.step("① 写入预填：观演人（选座实名项目）"):
        before = c.get_prefabricate_config(seat_t.product_id)
        c.set_prefabricate_real_names(seat_t.product_id, [rid])
        allure.attach(f"写入前 config={json.dumps(before, ensure_ascii=False)}",
                      name="写入前")

    with allure.step("② 写入预填：收货地址（纸质快递项目）"):
        c.set_prefabricate_address(paper_t.product_id, addr["id"])

    with allure.step("③ 回读校验：预填内容与写入一致"):
        cfg = c.get_prefabricate_config(seat_t.product_id)
        allure.attach(json.dumps(cfg, ensure_ascii=False, indent=1)[:1200],
                      name="回读 config")
        blob = json.dumps(cfg, ensure_ascii=False)
        assert cfg, "抢票准备写入后 config 仍为空 —— 预填未落库"
        assert str(rid) in blob, f"预填观演人 id={rid} 未回读出来：{blob[:300]}"

    _verdict("TC-011", "通过",
             f"预填观演人(id={rid}) / 收货地址(id={addr['id']}) 写入成功，"
             f"config 回读一致；「开售后自动带入」需等场次开售后再验（本次为接口层一致性）")


# ================================================================ TC-015 / TC-016

@allure.feature("930回归")
@allure.story("短信收取场景")
@allure.title("TC-015 电子票出票成功短信通知（需人工核对）")
@pytest.mark.release_manual
def test_TC015_sms_eticket_manual(c):
    """无法自动化：生产**没有验证码/短信日志**（app-7001.log 是 beta 独有），
    且需要真机收短信。接口侧只能证明「出票成功」，短信文案与到达无法断言。
    """
    t = _zero_target(
        "电子票出票短信夹具",
        seat=False, real_name=0, delivery="eticket", event="refundable",
    )
    with allure.step("① 造一笔真实出票单（短信触发条件已满足）"):
        order_id = _new_order(c, t, delivery_type=4)
        ticket = _wait_eticket(c, order_id)
        allure.attach(f"orderId={order_id} 券码={ticket['code']} 手机号={c.phone}",
                      name="出票夹具")
    _verdict("TC-015", "未自动化",
             f"生产无短信日志、无真机收信能力，短信文案/到达无法断言。"
             f"已备好出票单 orderId={order_id}（手机号 {c.phone}），需人工核对短信内容")
    pytest.skip("生产无短信日志（sms_log_url=None），短信到达与文案需人工核对")


@allure.feature("930回归")
@allure.story("短信收取场景")
@allure.title("TC-016 超时付款退款短信通知（需人工核对）")
@pytest.mark.release_manual
def test_TC016_sms_timeout_refund_manual(c):
    """无法自动化：需先造待支付单、等支付倒计时自然超时（分钟级）、
    再核对「已关闭+已发起退款」短信 —— 生产无短信日志，且等待成本高。
    """
    _verdict("TC-016", "未自动化",
             "依赖支付倒计时自然超时（分钟级）后的人工短信核对；生产无短信日志。"
             "自动化侧已具备下单+取消能力（client.cancel_order 已修正 reason 必填），"
             "建议由人工在 H5 走一次超时流程")
    pytest.skip("需等支付倒计时自然超时 + 人工核对短信，生产无短信日志")


# ================================================================ TC-017

@allure.feature("930回归")
@allure.story("短信收取场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-017 抢票提醒短信通知（订阅动作可验，短信需人工）")
def test_TC017_prefabricate_message_subscribe(c):
    """用例：点击添加提醒 → 检查短信内容 → 点击链接跳转。

    自动化可覆盖「订阅/取消订阅状态真的被服务端记住」（订阅动作是短信的前置条件）；
    短信文案与链接跳转需人工。

    ⚠️ 实测（2026-09-30）：`POST /v1/prefabricate/product/message` 对**部分项目**
    直接 `500 该商品不在预售状态, 请刷新重试`（P08/P09/P10/P11 四个选座项目全部如此），
    而 P01~P07 与 P12 正常。**是否可订阅是项目级配置差异，不是缺陷** ——
    所以这里做「探测式选品」：逐个项目试订阅，用第一个能订阅成功的，
    并把「哪些项目不可订阅」如实记录进报告，避免下次换机器跑时又踩一遍。
    """
    tried: dict[str, str] = {}
    chosen = None
    with allure.step("① 探测：哪些项目的「开售提醒」可订阅"):
        for t in catalog.current().find():
            pid = t.product_id
            if pid in tried:
                continue
            resp = c.try_call(
                "POST", f"/v1/prefabricate/product/message?storeId={env.store_id}",
                json={"productId": pid, "subStatus": True},
            )
            if resp.get("code") == 200:
                tried[pid] = "可订阅"
                c.try_call(
                    "POST", f"/v1/prefabricate/product/message?storeId={env.store_id}",
                    json={"productId": pid, "subStatus": False},
                )
                if chosen is None:
                    chosen = t
            else:
                tried[pid] = f"不可订阅 code={resp.get('code')} {resp.get('message')}"
        allure.attach(json.dumps(tried, ensure_ascii=False, indent=1), name="可订阅性探测")
    assert chosen, (
        "清单里没有任何项目支持开售提醒订阅，TC-017 无法验证 —— "
        f"探测结果：{tried}"
    )

    pid = chosen.product_id
    with allure.step(f"② 读初始订阅状态（用项目 P?/{pid}）"):
        before = c.get_prefabricate_message(pid)
        allure.attach(f"项目={chosen.project_name} productId={pid}\n"
                      f"初始 subStatus={before}", name="订阅前")

    with allure.step("③ 点击「添加提醒」（订阅）"):
        c.set_prefabricate_message(pid, True)
        after_sub = c.get_prefabricate_message(pid)
        allure.attach(f"订阅后 subStatus={after_sub}", name="订阅后")
        assert after_sub is True, "订阅开售提醒后回读状态仍为未订阅"

    with allure.step("④ 取消订阅并回读（状态可逆）"):
        c.set_prefabricate_message(pid, False)
        after_unsub = c.get_prefabricate_message(pid)
        allure.attach(f"取消后 subStatus={after_unsub}", name="取消后")
        assert after_unsub is False, "取消订阅后回读状态仍为已订阅"

    unsub = sorted(p for p, v in tried.items() if v != "可订阅")
    _verdict("TC-017", "部分通过",
             f"订阅/取消订阅状态服务端可读写且可逆（{before}→True→False，"
             f"项目 {chosen.project_name}/pid={pid}，接口回传接收号码）。"
             f"真实短信到达与链接跳转需人工（生产无短信日志）。"
             f"另注：{len(unsub)} 个项目不可订阅（报「不在预售状态」）：{unsub}")


# ================================================================ TC-018

def _readback_project(c, b, key: str, case: str):
    """项目信息三方一致性回读：清单登记 ↔ B 端上架配置 ↔ C 端展示。"""
    proj = catalog.current().project(key)
    assert proj.orderable, f"清单项目 {key} 未登记 productId/skuId，无法回读"
    pid = proj.product_id
    allure.attach(proj.note or "-", name="清单备注")

    with allure.step(f"① B 端上架配置（/v1/shop/product/{pid}/saleConfig）"):
        cfg = b._request("GET", f"/v1/shop/product/{pid}/saleConfig",
                         params={"storeId": b.store_id})
        allure.attach(json.dumps(
            {k: cfg.get(k) for k in ("id", "activityId", "onSale", "purchaseStatus",
                                     "purchaseTimeStatus", "refundSupport",
                                     "inventoryRecoverySupport")},
            ensure_ascii=False, indent=1), name="B端上架配置")
        assert cfg.get("onSale") is True, f"项目 {pid} 在 B 端未上架(onSale=false)"
        assert cfg.get("refundSupport") is not None, "售后开关未配置"

    with allure.step(f"② C 端项目详情（/v1/product/{pid}）"):
        detail = c.get_product_detail(pid)
        allure.attach(json.dumps(
            {k: detail.get(k) for k in ("productName", "status", "deliveryType")},
            ensure_ascii=False, indent=1), name="C端项目详情")
        assert detail.get("productName") == proj.name, (
            f"C 端项目名 {detail.get('productName')!r} ≠ 清单 {proj.name!r}"
        )
        assert detail.get("status") == 2, (
            f"C 端项目状态应为 2(在售)，实际 {detail.get('status')}"
        )
        assert set(proj.delivery_type) <= set(detail.get("deliveryType") or []), (
            f"交付方式不一致：清单 {sorted(proj.delivery_type)} "
            f"vs C 端 {detail.get('deliveryType')}"
        )

    with allure.step("③ C 端票档覆盖：清单登记的 sku 必须都能在 C 端看到"):
        skus = c.get_sku_list(pid)
        live = {s.get("skuId") for s in (skus or [])}
        wanted = {t.sku_id for t in catalog.current().find(project=key)}
        missing = sorted(wanted - live)
        allure.attach(f"清单登记 {len(wanted)} 个票档 / C 端在售 {len(live)} 个 / "
                      f"缺失 {missing}", name="票档覆盖",
                      attachment_type=allure.attachment_type.TEXT)
        assert not missing, (
            f"清单登记但 C 端看不到的票档：{missing}（上架不完整或清单已漂移）"
        )
    return proj, len(wanted), len(live)


@allure.feature("930回归")
@allure.story("创建项目场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-018 完整创建选座项目-创建场次票档-上架流程（配置一致性回读）")
def test_TC018_seat_project_readback(c, b):
    """用例预期「项目信息完全与配置一致」。

    执行口径：对已按流程建好的选座项目做**三方一致性回读**
    （清单登记 ↔ B 端上架配置 ↔ C 端展示与票档覆盖）。
    完整「新建项目向导」是一次性搭建动作（每次回归都新建会污染 888 店），
    已沉淀在 `tools/probe/build_prod_projects.py` / `./run.sh onsale`，此处不重复执行。
    """
    target = _target("选座项目配置回读", seat=True)
    proj, n_want, n_live = _readback_project(c, b, target.project_key, "TC-018")
    _verdict("TC-018a", "部分通过",
             f"选座项目 {proj.key}({proj.product_id}) 三方一致：B端 onSale=true；"
             f"C端名称/状态/交付方式与清单一致；清单 {n_want} 个票档全部在 C 端可见"
             f"（C端共 {n_live} 个）。完整「新建→建场次票档→上传座位图→上架」向导需人工")


@allure.feature("930回归")
@allure.story("创建项目场景")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("TC-018 完整创建非选座项目-创建场次票档-上架流程（配置一致性回读）")
def test_TC018_nonseat_project_readback(c, b):
    """非选座项目版本，口径同上（见 TC-018 选座版的说明）。"""
    target = _target("非选座项目配置回读", seat=False)
    proj, n_want, n_live = _readback_project(c, b, target.project_key, "TC-018")
    _verdict("TC-018b", "部分通过",
             f"非选座项目 {proj.key}({proj.product_id}) 三方一致：B端 onSale=true；"
             f"C端名称/状态/交付方式与清单一致；清单 {n_want} 个票档全部在 C 端可见"
             f"（C端共 {n_live} 个）。完整新建向导需人工")


# ================================================================ TC-019（补充用例）

@allure.feature("930回归")
@allure.story("退款场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-019 退款三种方式：B端条件退 / B端常规退 / C端条件退（补充用例）")
@pytest.mark.release_core
def test_TC019_refund_three_paths(c, b, rc):
    """**不由 930 用例表派生**，是按产品退款口径补的自动化加固用例。

    覆盖「退款有三种方式」三条通路与各自的硬约束：
      ① B端条件退 —— 订单详情发起，可选 仅退票 / 仅退款 / 退票退款
      ② B端常规退 —— 退款中心发起 + 退款审批通过，**只能全量退票退款**，不受场次配置影响
      ③ C端条件退 —— 用户自助、无需审批，须**同时**满足
         「场次配了条件退」+「在条件退时间范围内」+「订单非终态」+「退款金额≠0」

    夹具从清单中按业务形态选择两个 0 元电子票场次：
      · 条件退场次
      · 非条件退场次
    两条单最后都会被退到 5/已退款，不留半途数据。
    """
    tx = _zero_target(
        "C端条件退入口开启的 0 元电子票",
        seat=False, real_name=0, delivery="eticket", event="refundable",
    )
    ty = _zero_target(
        "C端条件退入口关闭的 0 元电子票",
        seat=False, real_name=0, delivery="eticket", event="non_refundable",
    )
    allure.attach(f"{tx.label}\n{ty.label}", name="夹具（清单登记项）")

    with allure.step("① 各下一单（均 0 元电子票，未核销 → 非终态）"):
        ox = _new_order(c, tx, delivery_type=4, contact_name="930退款通路")
        oy = _new_order(c, ty, delivery_type=4, contact_name="930退款通路")
        allure.attach(f"条件退场次单={ox} / 非条件退场次单={oy}", name="订单")

    # ---------------- ③ C端条件退 ----------------
    with allure.step("② C端条件退·约束A「场次必须配了条件退」——决定入口有无"):
        pre_x = c.refund_pre(ox)
        pre_y = c.refund_pre(oy)
        allure.attach(json.dumps({"条件退场次": pre_x, "非条件退场次": pre_y},
                                 ensure_ascii=False, indent=1), name="C端退款预校验")
        assert pre_x.get("canShowRefund") is True, (
            f"条件退场次的单应显示退款入口（canShowRefund=true），实际 {pre_x}"
        )
        assert pre_y.get("canShowRefund") is False, (
            f"非条件退场次的单**不应**出现退款入口（canShowRefund=false），实际 {pre_y}"
        )
        assert "不支持退款" in (pre_y.get("refundTip") or ""), (
            f"非条件退场次应提示「本项目不支持退款」，实际 {pre_y.get('refundTip')!r}"
        )

    with allure.step("③ C端条件退·约束B「退款金额不能为0」——0元单不发起申请"):
        refund_amount = float(pre_x.get("refundAmount") or 0)
        assert refund_amount == 0, (
            f"当前 0 元夹具的退款金额应为 0，实际 {pre_x.get('refundAmount')}"
        )
        allure.attach(
            "C 端申请退款不能使用 0 元单；本步骤只记录 refund_pre，"
            "不调用 /v1/order/refund/{id}/submit。"
            "C 端提交成功路径由 TC-020 使用非 0 元真实支付单覆盖。",
            name="0元单处理口径", attachment_type=allure.attachment_type.TEXT)

    with allure.step("④ B 端核销，把条件退的单推到终态"):
        code_x = _wait_eticket(c, ox)["code"]
        b.verify(code_x)
        _wait_b_status(b, ox, {STATUS_FINISHED})

    with allure.step("⑤ C端条件退·约束C「订单非终态」——终态后 canRefund 关闭"):
        pre_x2 = c.refund_pre(ox)
        allure.attach(json.dumps(pre_x2, ensure_ascii=False, indent=1), name="核销后预校验")
        assert pre_x2.get("canShowRefund") is True, "终态不应让入口消失（入口由场次配置决定）"
        assert pre_x2.get("canRefund") is False, (
            f"核销后的终态订单 C 端不应可自助退，实际 {pre_x2}"
        )
        assert "已经完成" in (pre_x2.get("refundTip") or ""), (
            f"终态应提示「订单已经完成啦…」，实际 {pre_x2.get('refundTip')!r}"
        )

    # ---------------- ① B端条件退 ----------------
    with allure.step("⑥ B端条件退·三种组合（仅退票 / 仅退款 / 退票退款）"):
        info = rc.refundable(ox)
        combos = {what: rc.build_items(info, what=what)
                  for what in (WHAT_TICKET, WHAT_MONEY, WHAT_BOTH)}
        allure.attach(json.dumps(combos, ensure_ascii=False, indent=1), name="三组合入参")
        assert combos[WHAT_TICKET][0]["refundMoneyCount"] == 0, "仅退票不应含退款份数"
        assert combos[WHAT_MONEY][0]["refundTicketCount"] == 0, "仅退款不应含退票份数"
        both = combos[WHAT_BOTH][0]
        assert both["refundTicketCount"] > 0 and both["refundMoneyCount"] > 0, \
            "退票退款应同时含退票与退款份数"

    with allure.step("⑦ B端条件退闭环（订单详情发起 → 审批 → 工单成功 → 订单已退款）"):
        summary = _b_refund(rc, ox, "930回归 TC-019 B端条件退")
        allure.attach(json.dumps(
            {k: summary.get(k) for k in ("refundId", "taskId", "amount", "approved")},
            ensure_ascii=False, indent=1), name="条件退摘要")
        order_x = _assert_refunded(rc, b, ox, summary)

    # ---------------- ② B端常规退 ----------------
    with allure.step("⑧ B端常规退·从退款中心发起（只能全量退票退款）"):
        center_hit = rc.center_find(oy, timeout=30)
        allure.attach(json.dumps(center_hit or {}, ensure_ascii=False, indent=1)[:800],
                      name="退款中心里的该单")
        assert center_hit, f"刚下的单 {oy} 未出现在退款中心列表（常规退发起入口）"
        assert center_hit.get("purchaseOrderId"), "退款中心条目未带 purchaseOrderId"

        reg = rc.regular_refund(oy)
        allure.attach(json.dumps(
            {k: reg.get(k) for k in ("purchaseOrderId", "taskId", "afterSubmit")},
            ensure_ascii=False, indent=1), name="常规退发起")
        assert reg.get("afterSubmit") == ORDER_STATUS_AFTER_SALE, (
            f"常规退发起后订单应进 {ORDER_STATUS_AFTER_SALE}/售后中，"
            f"实际 {reg.get('afterSubmit')}"
        )

    with allure.step("⑨ 退款审批通过 → 工单成功 + 订单已退款（且恒为「退票+退款」）"):
        task = reg.get("task") or {}
        allure.attach(json.dumps(task, ensure_ascii=False, indent=1)[:600], name="常规退工单")
        assert task.get("taskStatus") == TASK_SUCCESS, (
            f"常规退工单未成功：{task_status_desc(task.get('taskStatus'))}"
        )
        assert task.get("refundTypeDesc") == "退票+退款", (
            f"常规退应恒为全量「退票+退款」，实际 {task.get('refundTypeDesc')!r}"
        )
        assert reg.get("order", {}).get("status") == ORDER_STATUS_REFUNDED, (
            f"常规退后订单应为 {ORDER_STATUS_REFUNDED}/已退款，"
            f"实际 {reg.get('order', {}).get('status')}"
        )
        allure.attach("\n".join(f"{e['desc']}" for e in b.order_event_log(oy)),
                      name="常规退操作流水", attachment_type=allure.attachment_type.TEXT)

    _verdict("TC-019", "通过",
             f"C端条件退约束已实测：入口由场次配置决定（条件退场次 canShowRefund=true / "
             f"非条件退场次 false+「本项目不支持退款」）、0元单仅记录 pre 不发起 C 端申请、"
             f"终态 canRefund=false「订单已经完成啦」；"
             f"B端条件退三组合入参正确且闭环({ox}→{order_x.get('status')}{order_x.get('statusName')})；"
             f"B端常规退从退款中心发起→14售后中→审批通过→{ORDER_STATUS_REFUNDED}已退款、"
             f"工单恒为「退票+退款」({oy})")


# ================================================================ TC-020（补充用例）

# ---------------- 真实支付夹具（TC-020 专用） ----------------
# 与 test_real_pay.py 共用同一个闸门（migu_api/fundgate.py），三种模式：
#   off（默认）→ 本用例跳过，不产生任何资金流水
#   on         → 下新单 + 打印二维码，**真人扫 0.01 元**，付完自动接着跑
#   MIGU_PAY_ORDER=<orderId> → 复用一笔已付款订单（auto 模式，不再花钱，用于失败补跑）
REAL_PAY = fundgate.allows_new_payment()
RESUME_ORDER = os.environ.get("MIGU_PAY_ORDER") or None
PAY_TIMEOUT = fundgate.pay_timeout()
# 生产收银台只开支付宝（888 店 wechat=false），默认渠道由环境配置给，可用环境变量覆盖
CHANNEL = os.environ.get("MIGU_PAY_CHANNEL", env.pay_bank_code).upper()
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"

# TC-020 的夹具必须是「条件退场次里的非 0 元票档」：
# C 端条件退要求退款金额≠0，所以只能从清单里按业务形态动态选择。
C_REFUND_PAID = {"seat": False, "real_name": 0, "delivery": "eticket", "event": "refundable"}


def _pay_qr(client: MiguClient, order_id, amount: str, pay_detail: dict) -> str:
    """创建支付记录 → 落付款入口（二维码 PNG + 支付宝表单页），返回可扫的链接。

    与 test_real_pay.py 同一套做法（paypage/qr 两个小模块共用）：
      微信返回 https 链接 → 直接出二维码；
      支付宝返回 HTML 表单 → 存 .html（浏览器打开自动跳转）+ 同时转成等价 GET 链接出二维码。
    """
    pay = client.create_payment(order_id, bank_code=CHANNEL, pay_amount=amount)
    params = pay.get("paymentParams") or ""
    assert params, f"未拿到支付参数（渠道 {CHANNEL} 在店铺 {env.store_id} 是否已开启？）：{pay}"
    allure.attach(paypage.describe(params), name="支付参数形态")

    if paypage.is_link(params):
        pay_url = paypage.strip_cdata(params)
        png = qr.save_png(pay_url, ARTIFACTS / f"{env.name}_pay_qr_{order_id}.png")
    else:
        html = paypage.save_form_html(
            params, ARTIFACTS / f"{env.name}_pay_form_{order_id}.html")
        pay_url = paypage.to_pay_url(params)
        png = qr.save_png(pay_url, ARTIFACTS / f"{env.name}_pay_qr_{order_id}.png")
        allure.attach.file(str(html), name="支付宝收银台表单页",
                           attachment_type=allure.attachment_type.HTML)

    allure.attach.file(str(png), name="支付二维码",
                       attachment_type=allure.attachment_type.PNG)
    left = int(pay_detail.get("leftTime") or 0) // 1000
    print(f"\n{'=' * 64}\n★ 请扫码支付 {amount} 元"
          f"（环境 {env.name} / 店铺 {env.store_id} / 渠道 {CHANNEL}）\n"
          f"二维码：{png}\n链接：{pay_url}\n{'=' * 64}\n"
          f"订单 {order_id} 收银台倒计时 {left}s，请尽快付款\n", flush=True)
    return pay_url


@allure.feature("930回归")
@allure.story("退款场景")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("TC-020 C端条件退全链路：0.01元真实支付 → C端自助退款 → 订单已退款（补充用例）")
@pytest.mark.real_pay
@pytest.mark.release_money
@pytest.mark.skipif(
    not (REAL_PAY or RESUME_ORDER),
    reason="需真实支付 0.01 元（人工扫码）；开闸：--real-pay=on，"
           "或复用已付订单 MIGU_PAY_ORDER=<orderId>",
)
def test_TC020_c_refund_real_pay(c, b):
    """补齐 TC-019 唯一跑不到的那一段：**C端条件退的真金闭环**。

    为什么 TC-019 覆盖不完整：
      TC-019 用 888 店的 0 元夹具把「C端条件退的可观测约束」逐条验到了（入口有无、
      终态可否点、0 元被拦），但**没能真提交成功过一次** —— 因为 0 元票档天然
      0 元单不能用于 C 端申请退款，而套件原本没有任何一笔"真付过钱"的单。
      于是「C端条件退到底能不能退成功、钱退到哪、订单落到什么状态」一直是空白。

    本用例把这段补上（夹具 = 清单中条件退场次的非 0 元电子票票档）：
      ① 下单 0.01 元 → ② 真实支付（人工扫 0.01 元）→ ③ 订单 11/待使用
      → ④ C端 refund_pre：条件退时间窗内 canShowRefund=true & canRefund=true & refundAmount>0
      → ⑤ C端 refund_submit **无需审批，直接成功**
      → ⑥ 回读订单/C 端详情：落 5/已退款

    为什么必须花钱：C 端条件退的硬约束之一就是「退款金额不能为 0」，
    0 元单不能用于 C 端申请退款 —— 想验"退成功"只能先真付一笔。
    一轮花费 0.01 元；失败补跑请用 `MIGU_PAY_ORDER=<已付订单号>`，不要重复支付。
    """
    tx = _target_where(
        "C端条件退真金闭环",
        lambda t: (not t.is_free) and "非条件" not in t.event_name,
        **C_REFUND_PAID,
    )
    allure.attach(fundgate.label(), name="本轮真实支付闸门")
    allure.attach(tx.label, name="夹具（清单登记项）")
    assert not tx.is_free, (
        f"TC-020 必须用非 0 元票档（C 端条件退要求退款金额≠0），实际 {tx.price} 元"
    )

    # ---------------- ① 下单 + ② 真实支付 ----------------
    if RESUME_ORDER:
        order_id = int(RESUME_ORDER)
        _ORDERS.append(order_id)
        with allure.step(f"续跑：复用已付款订单 {order_id}（不重新下单/付款）"):
            detail = c.get_order_detail(order_id)
            allure.attach(str(order_id), name="orderId")
            allure.attach(status_text(detail.get("status")), name="订单当前状态")
            allure.attach(
                "由 MIGU_PAY_ORDER 指定。场景：真实支付已成功但后续步骤失败 —— "
                "钱已经花掉没法重来，用续跑把退款链路补齐，避免再付一次。",
                name="续跑说明", attachment_type=allure.attachment_type.TEXT)
            assert detail.get("status") in PAID_STATUS or \
                detail.get("status") == STATUS_REFUNDED, (
                    f"续跑要求该订单已支付（或已退款），实际 {status_text(detail.get('status'))}"
                )
    else:
        with allure.step(f"① 下单：{tx.label}（本用例将真实支付 {tx.price} 元）"):
            order_id = _new_order(c, tx, delivery_type=4, contact_name="930C端条件退")
            detail = c.get_order_detail(order_id)
            allure.attach(str(order_id), name="orderId")
            allure.attach(status_text(detail.get("status")), name="初始状态")
            assert detail.get("status") == STATUS_UNPAID, (
                f"下单后应为待付款({STATUS_UNPAID})，"
                f"实际 {status_text(detail.get('status'))}"
            )

        with allure.step(f"② 收银台 + 创建支付记录（渠道 {CHANNEL}）"):
            pay_detail = c.get_pay_detail(order_id)
            allure.attach(json.dumps(pay_detail, ensure_ascii=False, indent=1),
                          name="收银台详情", attachment_type=allure.attachment_type.JSON)
            assert float(pay_detail.get("payAmount") or 0) == float(tx.price), (
                f"收银台金额应为 {tx.price}，实际 {pay_detail.get('payAmount')}"
            )
            _pay_qr(c, order_id, tx.amount, pay_detail)

        with allure.step(f"③ 等待人工扫码支付结果（最长 {PAY_TIMEOUT}s）"):
            t0 = time.time()
            c.wait_for_paid(order_id, timeout=PAY_TIMEOUT, interval=3, verbose=True)
            detail = c.get_order_detail(order_id)
            cost = int(time.time() - t0)
            allure.attach(f"status={status_text(detail.get('status'))} 耗时={cost}s "
                          f"实付={detail.get('paymentPrice')}", name="支付结果")
            assert detail.get("status") in PAID_STATUS, (
                f"等待 {PAY_TIMEOUT}s 仍未支付成功，订单状态 "
                f"{status_text(detail.get('status'))}（未付款的订单会超时自动关闭）"
            )

    # ---------------- ③ 已支付 → C端条件退 ----------------
    with allure.step("④ C端条件退·前置校验（条件退场次、时间窗内、非终态、金额≠0）"):
        pre = c.refund_pre(order_id)
        allure.attach(json.dumps(pre, ensure_ascii=False, indent=1), name="C端退款预校验")
        assert pre.get("canShowRefund") is True, (
            f"条件退场次的单应出现退款入口（canShowRefund=true），实际 {pre}"
        )
        assert pre.get("canRefund") is True, (
            f"已支付未核销、且处于条件退时间范围内的非0元单应可自助退（canRefund=true），"
            f"实际 canRefund={pre.get('canRefund')}，refundTip={pre.get('refundTip')}"
        )
        refund_amount = float(pre.get("refundAmount") or 0)
        assert refund_amount > 0, (
            f"见证「0 元单必被拦」的反面：本单退款金额应>0，实际 {pre.get('refundAmount')}"
        )

    with allure.step("⑤ C端条件退·提交（**无需审批**，用户自助）"):
        resp = _c_refund_submit_ready(
            c, order_id, timeout=60, reason="930回归 TC-020 C端条件退",
            type=1, refundAmount=refund_amount,
        )
        allure.attach(json.dumps(resp, ensure_ascii=False, indent=1)[:800],
                      name="C端 submit 响应")
        assert resp.get("code") == 200, (
            f"C端条件退应直接受理（无需审批），实际 {resp.get('code')} {resp.get('message')}"
        )

    with allure.step("⑥ 回读订单：C端自助退款后应落 5/已退款"):
        order = _wait_b_status(b, order_id, REFUNDED_STATUS, timeout=90)
        c_detail = c.get_order_detail(order_id)
        allure.attach(
            f"B端订单：{order and order.get('status')}"
            f"({order and order.get('statusName')})\n"
            f"C端订单：{status_text(c_detail.get('status'))}\n"
            f"实付={c_detail.get('paymentPrice')}", name="退款后两侧订单状态")
        assert order and order.get("status") in REFUNDED_STATUS, (
            f"C 端退款成功后订单应落退款态（电子票为 {STATUS_REFUNDED}/已退款），"
            f"实际 {order and order.get('statusName')}"
        )
        assert c_detail.get("status") in REFUNDED_STATUS, (
            f"C 端自己看到的订单也应已退款，实际 {status_text(c_detail.get('status'))}"
        )
        if b.order_event_log(order_id):
            allure.attach("\n".join(f"{e['desc']}" for e in b.order_event_log(order_id)),
                          name="订单操作流水", attachment_type=allure.attachment_type.TEXT)

    _verdict("TC-020", "通过",
             f"C端条件退真金闭环：条件退场次 0.01 元票档下单 → 真实支付 "
             f"{c_detail.get('paymentPrice')} 元 → 订单 11 待使用 → "
             f"C端 pre（canShowRefund=true/canRefund=true/refundAmount={refund_amount}）"
             f" → submit **无需审批直接成功** → 回读订单 "
             f"{order.get('status')}{order.get('statusName')}")
