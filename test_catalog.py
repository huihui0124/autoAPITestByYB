# -*- coding: utf-8 -*-
"""
test_catalog.py —— 「测试专用商品清单」自检（纯本地，不打环境，秒级）

守的是这套机制本身：
    1. 清单文件格式合法、两个环境段都齐、项目编号不漏；
    2. config 派生出来的商品（fixture_free_product / paid_test_product）与清单一致
       —— 防止"改了清单但用例还在用老 ID"这种最难查的漂移；
    3. 白名单闸门真的能拦：清单外的 (productId, skuId) 必须被拒；
    4. 按用途 / 维度查找的语义正确（free_fixture、paid 全局唯一，seat/实名/交付可筛）。

跑法：
    ./.venv/bin/pytest test_catalog.py -v
    ./.venv/bin/pytest -m catalog
"""
import allure
import pytest

from migu_api import catalog
from migu_api.config import PROD, TEST

pytestmark = [pytest.mark.catalog]

CORE_KEYS = [f"P{i:02d}" for i in range(1, 9)]   # P01..P08

_EMPTY_REASON = ("「测试专用商品清单」当前为空 —— 这是初始状态：等 B 端把测试项目"
                 "建好后，用 ./run.sh catalog add 登记，覆盖度检查会自动恢复。")


@pytest.fixture(scope="module")
def test_cat():
    return catalog.load("test")


@pytest.fixture(scope="module")
def prod_cat():
    return catalog.load("prod")


def _require_populated(cat):
    """清单为空（等待登记）时跳过覆盖度类断言；登记后自动生效。"""
    if not cat.projects:
        pytest.skip(_EMPTY_REASON)


# ---------------------------------------------------------------- 格式与完整性

@allure.feature("测试商品清单")
@allure.story("清单自检")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("清单文件存在、JSON 合法、test/prod 两个环境段齐全")
@pytest.mark.release_core
def test_catalog_loadable(test_cat, prod_cat):
    with allure.step("确认清单来源与版本"):
        allure.attach(f"文件：{test_cat.source}\n版本：v{test_cat.version}\n"
                      f"更新：{test_cat.updated}",
                      name="清单来源", attachment_type=allure.attachment_type.TEXT)
    assert test_cat.source.exists(), f"清单文件不存在：{test_cat.source}"
    assert test_cat.version, "清单缺少 version 字段"
    assert test_cat.store_id == "1", f"测试环境 storeId 应为 1，实际 {test_cat.store_id}"
    assert prod_cat.store_id == "888", (
        f"生产测试店 storeId 应为 888（绝不允许写 1），实际 {prod_cat.store_id}")


@allure.feature("测试商品清单")
@allure.story("清单自检")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("8 个核心项目（P01-P08）在两个环境都有登记")
@pytest.mark.parametrize("cat_name", ["test", "prod"])
@pytest.mark.release_core
def test_core_projects_present(cat_name):
    cat = catalog.load(cat_name)
    _require_populated(cat)
    keys = {p.key for p in cat.projects}
    missing = [k for k in CORE_KEYS if k not in keys]
    allure.attach("、".join(sorted(keys)), name=f"[{cat_name}] 已登记项目")
    assert not missing, f"[{cat_name}] 缺少核心项目：{missing}"


@allure.feature("测试商品清单")
@allure.story("清单自检")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("每个可下单票档字段完整且自洽（skuId/价格/项目号）")
@pytest.mark.parametrize("cat_name", ["test", "prod"])
@pytest.mark.release_core
def test_targets_wellformed(cat_name):
    cat = catalog.load(cat_name)
    problems = []
    for t in cat.targets():
        if not isinstance(t.sku_id, int):
            problems.append(f"{t.project_key}: skuId 非整数 {t.sku_id!r}")
        try:
            float(t.price)
        except (TypeError, ValueError):
            problems.append(f"{t.project_key}/{t.sku_id}: 价格无法解析 {t.price!r}")
        if t.status not in ("ready", "rework", "planned"):
            problems.append(f"{t.project_key}: 未知 status {t.status!r}")
        if t.delivery not in (None, "eticket", "paper", "idcard"):
            problems.append(f"{t.project_key}: 未知 delivery {t.delivery!r}")
        if t.real_name not in (None, 0, 1, 2):
            problems.append(f"{t.project_key}: 未知 realName {t.real_name!r}")
    allure.attach(str(len(cat.targets())), name=f"[{cat_name}] 可下单目标数")
    assert not problems, "清单字段有问题：\n" + "\n".join(problems)


@allure.feature("测试商品清单")
@allure.story("清单自检")
@allure.severity(allure.severity_level.NORMAL)
@allure.title("planned 项目不得进入白名单（没建好就不该被下单）")
@pytest.mark.parametrize("cat_name", ["test", "prod"])
@pytest.mark.release_core
def test_planned_is_not_orderable(cat_name):
    cat = catalog.load(cat_name)
    leaked = [t.label for t in cat.targets() if t.status == "planned"]
    assert not leaked, ("以下项目标着 planned（待新建）却进了白名单 —— "
                        "要么补 productId/skuId 改成 ready，要么清掉 SKU：\n"
                        + "\n".join(leaked))


# ---------------------------------------------------------------- config 一致性

@allure.feature("测试商品清单")
@allure.story("配置一致性")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("config 里的下单商品与清单一致（防止两处漂移）")
@pytest.mark.release_core
def test_config_matches_catalog(test_cat, prod_cat):
    with allure.step("比对测试环境"):
        expect_free = catalog.pair("test", "free_fixture")
        expect_paid = catalog.triple("test", "paid")
        allure.attach(f"清单 free_fixture={expect_free}\n"
                      f"config        ={TEST.fixture_free_product}\n"
                      f"清单 paid={expect_paid}\nconfig      ={TEST.paid_test_product}",
                      name="测试环境比对", attachment_type=allure.attachment_type.TEXT)
        assert TEST.fixture_free_product == expect_free
        assert TEST.paid_test_product == expect_paid

    with allure.step("比对生产环境"):
        assert PROD.fixture_free_product == catalog.pair("prod", "free_fixture")
        assert PROD.paid_test_product == catalog.triple("prod", "paid")


@allure.feature("测试商品清单")
@allure.story("配置一致性")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("免费夹具是 0 元、付费目标不是 0 元（否则部分退会假绿）")
@pytest.mark.release_core
def test_usage_semantics(test_cat):
    _require_populated(test_cat)
    free = test_cat.one(usage="free_fixture")
    paid = test_cat.one(usage="paid")
    with allure.step("校验免费夹具价格"):
        allure.attach(free.label, name="free_fixture")
    assert free.is_free, f"free_fixture 必须是 0 元免付票档，实际 {free.price} 元"
    with allure.step("校验付费目标价格"):
        allure.attach(paid.label, name="paid")
    assert not paid.is_free, (
        f"paid（真实支付/部分退用）不能是 0 元档 —— 0 元单不触发资金分支，"
        f"会让部分退的 bug 变假绿。实际 {paid.price} 元")


# ---------------------------------------------------------------- 白名单闸门

@allure.feature("测试商品清单")
@allure.story("下单白名单")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("清单内的目标全部放行")
@pytest.mark.release_core
def test_guard_allows_catalog_targets(test_cat):
    with allure.step(f"遍历 {len(test_cat.targets())} 个已登记目标"):
        for t in test_cat.targets():
            hit = catalog.guard_order(t.product_id, t.sku_id)
            assert hit is not None and hit.sku_id == t.sku_id
    allure.attach("\n".join(t.label for t in test_cat.targets()),
                  name="放行清单", attachment_type=allure.attachment_type.TEXT)


@allure.feature("测试商品清单")
@allure.story("下单白名单")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("清单外的商品/SKU 一律被拦（含同项目下的非法 SKU）")
@pytest.mark.release_core
def test_guard_blocks_outside(test_cat):
    known = {t.product_id for t in test_cat.targets()}
    bad_cases = [(999999, 1, "完全不存在的商品")]
    if known:
        bad_cases.append((sorted(known)[0], 99999999, "清单内项目 + 未登记 SKU"))
    for pid, sku, why in bad_cases:
        with allure.step(f"期望拦截：{why}（{pid}/{sku}）"):
            with pytest.raises(catalog.CatalogViolation) as ei:
                catalog.guard_order(pid, sku)
            msg = str(ei.value)
            allure.attach(msg, name=f"拦截原因 · {why}",
                          attachment_type=allure.attachment_type.TEXT)
            assert "测试专用商品清单" in msg


@allure.feature("测试商品清单")
@allure.story("下单白名单")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("闸门可临时关闭（MIGU_CATALOG_ENFORCE=0），但默认必须开启")
@pytest.mark.release_core
def test_enforce_switch(monkeypatch):
    with allure.step("默认（不设环境变量）应开启"):
        monkeypatch.delenv(catalog.ENFORCE_ENV, raising=False)
        assert catalog.enforce_enabled() is True

    with allure.step("显式关闭"):
        monkeypatch.setenv(catalog.ENFORCE_ENV, "0")
        assert catalog.enforce_enabled() is False
        assert catalog.guard_order(999999, 1) is None, "关闭后不应再拦"

    with allure.step("非法取值要报错，而不是静默放行"):
        monkeypatch.setenv(catalog.ENFORCE_ENV, "maybe")
        with pytest.raises(catalog.CatalogError):
            catalog.enforce_enabled()


# ---------------------------------------------------------------- 查找语义

@allure.feature("测试商品清单")
@allure.story("按需查找")
@allure.severity(allure.severity_level.CRITICAL)
@allure.title("按用途取唯一目标：free_fixture / paid 全清单各只有一条")
@pytest.mark.release_core
def test_unique_usages(test_cat):
    _require_populated(test_cat)
    for usage in ("free_fixture", "paid"):
        hits = test_cat.find(usage=usage)
        allure.attach("\n".join(h.label for h in hits), name=f"usage={usage}")
        assert len(hits) == 1, (
            f"usage={usage} 应全清单唯一（用例会用它取默认目标），实际命中 {len(hits)} 条："
            + "；".join(h.label for h in hits)
        )


@allure.feature("测试商品清单")
@allure.story("按需查找")
@allure.severity(allure.severity_level.NORMAL)
@allure.title("按维度查找：选座 / 实名档位 / 交付方式 / 场次 / 多次核销")
@pytest.mark.release_core
def test_find_by_dimension(test_cat):
    _require_populated(test_cat)
    cases = [
        ({"seat": True}, "选座项目"),
        ({"real_name": 0}, "非实名"),
        ({"real_name": 1}, "一票一证"),
        ({"real_name": 2}, "一单一证"),
        ({"delivery": "eticket"}, "电子票"),
        ({"delivery": "idcard"}, "身份证件入场"),
        ({"event": "refundable"}, "条件退场次"),
        ({"event": "non_refundable"}, "非条件退场次"),
        ({"event": "multi_verify"}, "多次核销场次"),
        ({"min_verify": 2}, "多次核销"),
    ]
    lines = []
    for kw, why in cases:
        hits = test_cat.find(**kw)
        lines.append(f"{why} {kw} → {len(hits)} 条")
        allure.attach("\n".join(h.label for h in hits), name=why,
                      attachment_type=allure.attachment_type.TEXT)
    # 这些维度在测试环境都必须至少命中一条，否则"按需查找"形同虚设
    assert test_cat.find(seat=True), "清单里应至少有一个选座项目"
    assert test_cat.find(min_verify=2), "清单里应至少有一个多次核销目标"
    allure.attach("\n".join(lines), name="查找统计",
                  attachment_type=allure.attachment_type.TEXT)


@allure.feature("测试商品清单")
@allure.story("按需查找")
@allure.severity(allure.severity_level.NORMAL)
@allure.title("找不到时的报错要能指导下一步（列出候选 / 指向 catalog.json）")
def test_lookup_error_is_actionable(test_cat):
    with pytest.raises(catalog.CatalogLookupError) as ei:
        test_cat.one(seat=True, real_name=2, delivery="idcard", project="F01")
    msg = str(ei.value)
    allure.attach(msg, name="报错文案", attachment_type=allure.attachment_type.TEXT)
    assert "找不到" in msg
    assert "catalog" in msg, "报错里要给出「怎么登记」的指引"


@allure.feature("测试商品清单")
@allure.story("按需查找")
@allure.severity(allure.severity_level.NORMAL)
@allure.title("清单可读文本能正常生成（run.sh catalog list 的后端）")
def test_describe(test_cat):
    text = test_cat.describe()
    allure.attach(text, name="清单文本", attachment_type=allure.attachment_type.TEXT)
    assert "测试专用商品清单" in text
    assert "可下单目标" in text


# ---------------------------------------------------------------- 空清单状态

@allure.feature("测试商品清单")
@allure.story("清单自检")
@allure.severity(allure.severity_level.BLOCKER)
@allure.title("空清单状态：白名单拦截一切下单、config 派生为 None、下游用例自动跳过")
def test_empty_catalog_state(test_cat):
    if test_cat.projects:
        pytest.skip("清单已登记项目，空态检查不适用（拦截/派生行为由其它用例守）")

    with allure.step("任何商品 ID 都不允许下单 —— 包括以前用过的老 ID"):
        for pid, sku in [(222837, 10636881), (222665, 10636741), (1, 1)]:
            with pytest.raises(catalog.CatalogViolation):
                catalog.guard_order(pid, sku)

    with allure.step("config 派生为 None（下游下单用例会自动 skip，不会乱下单）"):
        assert TEST.fixture_free_product is None, \
            f"空清单下 config 不应再登记夹具商品：{TEST.fixture_free_product}"
        assert TEST.paid_test_product is None, \
            f"空清单下 config 不应再登记付费商品：{TEST.paid_test_product}"
        assert catalog.pair("test", "free_fixture") is None
        assert catalog.triple("test", "paid") is None

    with allure.step("按用途查找的报错要指向登记动作"):
        with pytest.raises(catalog.CatalogError) as ei:
            catalog.one(usage="paid")
        msg = str(ei.value)
        allure.attach(msg, name="空清单报错文案",
                      attachment_type=allure.attachment_type.TEXT)
        assert "catalog" in msg or "清单" in msg, \
            "报错要能指导下一步（登记 / add 命令）"
