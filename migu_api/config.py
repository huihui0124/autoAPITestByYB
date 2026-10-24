# -*- coding: utf-8 -*-
"""
config.py —— 环境配置层（唯一的"哪里不同"登记处）

设计原则：
    - 测试/生产的一切差异（域名、证书、验证码获取方式、账号）只出现在这里
    - 用例代码、client 代码永远 import config，不写死任何环境信息
    - 切环境 = 换一个环境变量，不改一行代码：
        MIGU_ENV=test  .venv/bin/pytest ...   # 默认就是 test
        MIGU_ENV=prod  .venv/bin/pytest ...

    - 关键安全设计：verify_ssl / sms_log_url 这类"环境特有"配置
      在 prod 里要么关掉要么置空，误配会在启动时炸出来，而不是静默用错
"""
import os
from dataclasses import dataclass, field

from . import catalog as _catalog


@dataclass(frozen=True)
class EnvProfile:
    """一个环境差异点就是一个字段，加环境 = 加一个 profile"""

    name: str
    base_url: str            # 站点入口（H5 页面所在域）
    passport_base: str       # 通行证域名（登录跳转用）。测试/生产**不同域**，别混用
    api_prefix: str          # 接口前缀：实测 /v1/xx 真实路径是 {base_url}/saishi/api/v1/xx
    store_id: str            # 店铺 ID：beta 实测为 1（JS 配置里的 645 在测试环境不存在！）
    verify_ssl: bool         # 生产必须 True；测试环境自签证书只能 False
    sms_log_url: str | None  # 通行证验证码日志；生产没有这东西，置 None
    sms_code_ttl: int = 5 * 60
    http_timeout: int = 15
    accounts: dict = field(default_factory=dict)  # phone -> 用途备注
    # 「多次核销」特性是否已发布到该环境（口径：票据接口是否返回核销次数字段）。
    # 实测演进：
    #   · 测试环境：已上（票据返回 totalVerifyCount/usedVerifyCount/remainVerifyCount/
    #     verifyRecordList，场次可配 maxVerifyCount=3）
    #   · 生产 2026-09-29：票据只返回 code/status/expired、核销记录接口恒 [] → 判为未发布
    #   · 生产 2026-09-30 复跑：**票据已带次数字段、核销记录接口也有数据** → 开关过期，已翻正
    # ⚠️ 但仍有一层没发布：**生产 B 端场次编辑接口没有 maxVerifyCount 字段**
    #    （swagger /v1/selfProduct/events/edit 无该字段），所以生产所有场次核销次数恒为 1，
    #    "多次核销"这条业务能力在生产无法配置、也就无法覆盖。
    # 因此用例**不再依赖本开关**决定断言口径，改用运行时探测（见 test_b_verify.py），
    # 本字段只作环境说明/降级兜底，避免开关再次过期造成假红。
    supports_multi_verify: bool = False
    # 用于造"可核销夹具"的 0 元电子票商品 (productId, skuId)。
    # ★ 来源已收敛到「测试专用商品清单」catalog.json（用例登记 usage=free_fixture 的项目），
    #   这里只是把它派生出来，保持老用例 `env.fixture_free_product` 的写法不变。
    #   清单里没有 → None → 相关用例跳过（不再拿测试环境的 ID 去生产乱下单）。
    fixture_free_product: tuple | None = None
    # 下单/支付链路使用的付费商品 (productId, skuId, 金额字符串)。
    # ★ 同上，来源是 catalog.json 里 usage=paid 的那一条（清单内**必须唯一**）。
    #   被 test_core_chain.py（下单到网关受理）和 test_real_pay.py（真实付款）共用。
    #   清单里没有 → None → 这两条链路自动跳过。
    paid_test_product: tuple | None = None
    # 该环境收银台**默认可用**的支付渠道码。
    #   测试环境：微信(WX)/支付宝(AP) 都开着，默认 WX（出二维码最省事）
    #   生产环境：运行时配置 migu-store-runtime-config.production.json 里
    #             store 888 的 cashierPaymentMethods 是 alipay=true / wechat=false
    #             → **只有支付宝**，所以默认 AP。
    # 用例不再写死渠道，统一读这里；临时覆盖仍可用 MIGU_PAY_CHANNEL 环境变量。
    pay_bank_code: str = "WX"
    # 用例能否**自造 0 元多份夹具单**来覆盖"部分退"（详见 test_b_refund.py 的
    # `_provision_free_multi_order`）。0 元夹具商品下单即出票、无需付款，
    # 所以它让部分退能进**每次**全量回归，不用真人守着一笔付费单。
    # ⚠️ 生产必须为 False：实测（2026-09-29）生产 0 元单部分退会**正确**落
    #     15(部分退)（绿），而有价单会**错误**落 5(已退款)（红）——
    #     生产用 0 元单会把那个 bug 盖掉，是**假绿**，比跳过更危险。
    supports_free_multi_fixture: bool = True

    @property
    def api_base(self) -> str:
        """接口基地址，例如 https://union-passport.migu.cn/saishi/api"""
        return f"{self.base_url}{self.api_prefix}"

    @property
    def h5_url(self) -> str:
        """H5 站点入口（Referer / payRedirectUrl 用）。
        生产是 https://ticket.miguvideo.com/saishi/pages/tabs/index?storeId=888
        """
        return f"{self.base_url}/saishi/pages/tabs/index?storeId={self.store_id}"

    @property
    def api_host(self) -> str:
        """接口所在域名（不含协议）—— hmcer Cookie 必须挂在这个域上。

        ⚠️ 别用通行证域：测试环境两者同域（union-passport.migu.cn）所以以前没暴露问题；
        生产是 passport.migu.cn（通行证） vs ticket.miguvideo.com（接口），
        把 hmcer 挂在通行证域上，接口请求根本不会带上这个 Cookie。
        """
        return self.base_url.split("//")[-1].strip("/")

    @property
    def state_dir_name(self) -> str:
        """凭证缓存的子目录名 —— **必须按环境隔离**。

        否则 MIGU_ENV=prod 时会读到测试环境的 hmcer Cookie，
        轻则接口 401，重则把生产请求带着测试身份发出去。
        """
        return self.name


# ---------------- 各环境定义 ----------------
# 实测结论（2026-09-28，全部抓包验证过）：
#   1. Apifox 文档里的 /v1/xx 实际请求路径是 {base_url}/saishi/api/v1/xx
#   2. WAF 会 403 掉非浏览器 UA，必须带完整浏览器头 + Referer/Origin
#   3. beta 店铺 storeId=1；645 是 JS 里的默认值但测试环境不存在该店铺
#   4. 登录链路：通行证会话 → /login 302 发 ST token → POST /saishi/api/v3/login
#      换 hmcer Cookie → 之后所有业务接口靠 hmcer 鉴权
#   5. 通行证登录参数固定为 sourceid=203066（好麦通在通行证侧的应用 ID）

TEST = EnvProfile(
    name="test",
    base_url="https://union-passport.migu.cn",
    passport_base="https://union-passport.migu.cn",  # beta 环境通行证与站点同域
    api_prefix="/saishi/api",
    store_id="1",
    verify_ssl=False,                            # 自签证书 CN=localhost
    sms_log_url="https://union-passport.migu.cn/app-7001.log",
    accounts={
        "15550042537": "主测试账号（购票/下单链路）",
    },
    supports_multi_verify=True,                  # 测试环境已上多次核销
    # 下面两个值不再写死，统一从「测试专用商品清单」catalog.json 派生
    # （改商品只需改清单，不用碰代码）：
    #   fixture_free_product ← 项目 F01「测试ff」，0 元免付票档，下单即出票
    #   paid_test_product    ← 项目 P01 的「测试多次核销」场次，maxVerifyCount=3
    fixture_free_product=_catalog.pair("test", "free_fixture"),
    paid_test_product=_catalog.triple("test", "paid"),
    pay_bank_code="WX",                            # 测试环境微信/支付宝都开，默认微信
)

# 生产（2026-09-29 实测确认）：
#   - 站点 https://ticket.miguvideo.com，接口前缀 /saishi/api（与测试一致）
#   - 通行证域不同！生产是 passport.migu.cn（前端 bundle 里 beta/production 两个分支）
#   - **验证只允许在 storeId=888「咪咕生产测试店」**；storeId=1 是真实业务店「咪咕票务」，绝不可碰
#   - 生产没有验证码日志（app-7001.log 是 beta 独有）→ 生产登录必须另想办法
#   - 生产运行时配置（migu-store-runtime-config.production.json）里 store 888：
#         cashierPaymentMethods: alipay=true / wechat=false   ← 生产只有支付宝
#         DISABLE_MIGU_API_ENCRYPTION=1（接口不加密，省事）
#         HUMAN_VERIFICATION_MOCK=1（人机验证走 mock）
PROD = EnvProfile(
    name="prod",
    base_url="https://ticket.miguvideo.com",
    passport_base="https://passport.migu.cn",
    api_prefix="/saishi/api",
    store_id="888",                              # ★ 生产只能在这个"生产测试店"验证
    verify_ssl=True,                             # 生产证书是正规的，必须校验
    sms_log_url=None,                            # 生产没有验证码日志！
    # 生产 C 端账号（2026-09-29 实测校正）。
    #
    # 首选方式是**从浏览器直接取登录态**（不需要短信、也不需要真人守在旁边）：
    #     ./run.sh login --harvest
    # 只要你在浏览器里打开过生产 H5 并登录过，Cookie 库里就有 hmcer，
    # harvest 会解出来、打一次会员接口验证通过后才落盘。
    #
    # 备选方式是短信验证码（半自动）：
    #     ./run.sh login --phone 15550042537     # 触发真实短信，真人读码填入
    #     或 ./run.sh login --code 123456        # 码已从别处拿到
    # ⚠️ 实测 15550042537 这条短信通道会返 `{"code":20102,"message":"发送短信失败"}`
    #    （网关侧受理失败），所以生产目前实际是靠 18366267528 的浏览器登录态在跑。
    accounts={
        "18366267528": "生产 C 端实际登录账号（浏览器登录态 harvest 得到，stable）",
        "15550042537": "生产 C 端备选账号（短信通道当前报 20102 发送失败，暂不可用）",
    },
    # ⚠️ 生产票据接口**已带核销次数字段**（2026-09-30 实测：totalVerifyCount/
    #    usedVerifyCount/remainVerifyCount/verifyRecordList 都在，核销记录接口也有数据），
    #    但**场次级核销次数配置未发布**（B 端 events/edit 无 maxVerifyCount），
    #    所以生产所有票 totalVerifyCount 恒为 1 —— 只能验证"单次核销"。
    #    用例口径已改为运行时探测，本字段仅作说明。
    supports_multi_verify=True,
    # 生产测试店 storeId=888 的两个商品 —— **同样从 catalog.json 派生**
    # （清单 environments.prod 下：P01「paid_test_product」0.01 元、F01「fixture_free_product」0 元）。
    # 实测来源：B 端 /v1/selfProduct/list 查出，价格/在售正常。
    fixture_free_product=_catalog.pair("prod", "free_fixture"),
    paid_test_product=_catalog.triple("prod", "paid"),
    # 生产收银台只开支付宝 —— 微信通道在 888 店被显式关掉了（wechat=false）。
    # 写死 WX 会拿不到支付参数，所以这里必须是 AP。
    pay_bank_code="AP",
    # ★ 生产**不能**用 0 元夹具单跑部分退：实测 2026-09-29 生产 0 元 3 份单
    #   部分退 1 份 → 正确落 15(部分退)、剩 2 份可续退（**绿**）；
    #   而 0.01 元付费单部分退 → 错误落 5(已退款)、剩余不可退（**红**，即已知 bug）。
    #   差异出在"带资金"的退款分支上，所以生产一旦改用 0 元夹具就会**假绿**，
    #   把真 bug 盖住。生产的部分退只能靠真实付费多份单来暴露（已有 4 次复现记录）。
    supports_free_multi_fixture=False,
)

# 通行证登录跳转的固定参数（抓包确认，测试环境）
# 注意：绝不能带 forceAuthn=true！它会无视已有会话强制重新登录，
# 去掉后只要通行证会话有效，/login 每次 302 发新 ST token
PASSPORT_LOGIN_QUERY = (
    "sourceid=203066&sourceID=203066&apptype=2"
    "&isPassive=false&authType=DS&display=wap&logintype=1&needOneKey=1"
)

# 业务接口的公共请求头（WAF 校验 UA + Referer/Origin，缺一个就 403）
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_PROFILES = {"test": TEST, "prod": PROD}


# ---------------- B 端（商家后台 / 核销台）配置 ----------------
# B 端是另一套系统：域名、鉴权方式、接口前缀、状态码全都不一样，所以单独一个 dataclass。
# 实测确认（2026-09-29）：
#   1. 测试环境入口 http://ticket-test.miguvideo.com:18084（umi SPA）
#   2. 走 4A 单点登录，票据形如 ?iamcaspticket=xxx
#   3. 票据不能直接当 Cookie 用，必须换会话：
#      GET {api_base}/v1/user/ssoCallback?iamcaspticket=xxx  →  Set-Cookie hmid/ll/ct/dvid
#   4. 之后所有 B 端接口靠 Cookie + 请求头 hm-store-id 鉴权
#   5. 后端自带完整 OpenAPI 文档：GET {api_base}/v3/api-docs（625 个接口，比 Apifox 还全）


@dataclass(frozen=True)
class BEnvProfile:
    """B 端环境差异登记处。"""

    name: str
    base_url: str          # 站点入口
    api_prefix: str        # 接口前缀：测试是 /api；生产是 /b/api（见下方注释，这次踩过）
    store_id: str          # 店铺 ID，同时用于 query 和 hm-store-id 头
    front_prefix: str = ""        # 前端页面所在的路径前缀：测试 ""；生产 "/bfront"
    verify_timeout: int = 20      # 核销接口偶尔慢，给宽一点
    # 4A 单点登录门户（人工取票据时用；也可作为"自动续票"的入口）。
    # ⚠️ 这是**内网地址**，必须连上公司 VPN 才通（实测 2026-09-29 通）。
    # 测试环境 4A 门户与 B 端同机；生产 4A 门户是独立的一台内网机：
    #   门户  https://10.181.58.56:8444/amsportal/
    #   ├─ 302 → http://10.181.58.11:18090/casp/auth/login_mg.jsp?target=...&appCode=IAM000
    #   └─ 302 → https://10.181.58.11:8443/casp/auth/login_mg.jsp?target=...
    #            （CAS 认证服务，表单 POST 到 /casp/loginServlet.do，字段经 jCryption 加密）
    # 登录成功后门户里点「赛事票务管理平台」，就会带着 ?iamcaspticket= 跳回 B 端。
    four_a_portal: str | None = None

    @property
    def api_base(self) -> str:
        return f"{self.base_url}{self.api_prefix}"

    @property
    def front_url(self) -> str:
        """前端入口根地址 —— Referer / Origin 必须用它，否则网关可能拦。"""
        return f"{self.base_url}{self.front_prefix}"

    @property
    def home_url(self) -> str:
        """带 storeId 的后台首页，人工排查时用。"""
        return f"{self.front_url}/store/myStore/{self.store_id}"

    @property
    def state_dir_name(self) -> str:
        """B 端凭证缓存子目录名 —— 同样按环境隔离，防止测试/生产会话串用。"""
        return self.name


B_TEST = BEnvProfile(
    name="test",
    base_url="http://ticket-test.miguvideo.com:18084",
    api_prefix="/api",
    store_id="1",
    front_prefix="",
)

# 生产（2026-09-29 实测确认）：
#   前端页面： https://ticket.miguvideo.com/bfront/store/myStore/888
#   接口基地址：https://ticket.miguvideo.com/b/api
#       ↑ 前端 bundle 里硬编码 me="https://ticket.miguvideo.com/b/api"
#       ⚠️ 不是 /bfront/api（那条路径被 nginx 回落到 SPA index.html，返回 HTML），
#          也不是 /api（那是另一个后端，返回 Spring 404）
#   4A 票据入口：https://ticket.miguvideo.com/bfront/?iamcaspticket=xxx
#   页面用 window.routerBase="/bfront/"（测试环境是 "/"）
B_PROD = BEnvProfile(
    name="prod",
    base_url="https://ticket.miguvideo.com",
    api_prefix="/b/api",
    store_id="888",                # ★ 生产只能在这个"生产测试店"验证
    front_prefix="/bfront",
    # 生产 4A 门户（内网，需 VPN）。从这里进去点应用才会拿到新的 iamcaspticket。
    four_a_portal="https://10.181.58.56:8444/amsportal/",
)

_B_PROFILES = {"test": B_TEST, "prod": B_PROD}


def get_b_env() -> BEnvProfile:
    """读取 MIGU_B_ENV 返回 B 端 profile，默认 test。写错直接抛，不许静默跑错环境。"""
    name = os.environ.get("MIGU_B_ENV", "test").lower()
    if name not in _B_PROFILES:
        raise ValueError(
            f"未知 B 端环境 MIGU_B_ENV={name!r}，可选值: {', '.join(_B_PROFILES)}"
        )
    return _B_PROFILES[name]


# 全局单例：B 端配置入口
b_env = get_b_env()


def get_env() -> EnvProfile:
    """读取 MIGU_ENV 环境变量返回对应 profile，默认 test。

    写错环境名会直接抛异常——宁可启动失败，不能静默跑错环境。
    """
    name = os.environ.get("MIGU_ENV", "test").lower()
    if name not in _PROFILES:
        raise ValueError(
            f"未知环境 MIGU_ENV={name!r}，可选值: {', '.join(_PROFILES)}"
        )
    return _PROFILES[name]


# 全局单例：所有模块 from .config import env 用这一个入口
env = get_env()


# ---------------- 生产写操作闸门 ----------------

# 会**改变生产数据**的用例分组（下单 / 核销）。
# 生产环境默认一律拦掉，必须显式 MIGU_ALLOW_PROD_WRITE=1 才放行。
# run.sh 里有同样的闸门（更早一步拦），这里是给小范围直接跑 pytest 时兜底。
PROD_WRITE_ALLOW_ENV = "MIGU_ALLOW_PROD_WRITE"


def prod_write_blocked() -> bool:
    """当前是否应拦掉"会改生产数据"的用例。测试环境永远不拦。"""
    if env.name != "prod":
        return False
    return os.environ.get(PROD_WRITE_ALLOW_ENV) != "1"


def prod_write_skip_reason() -> str:
    return (
        "生产环境的写操作用例默认禁止执行（会在生产产生真实订单/核销记录）。"
        f"确认要在「生产测试店 storeId={PROD.store_id}」做写验证时，"
        f"设 {PROD_WRITE_ALLOW_ENV}=1 显式放行。"
        "只做只读自检请用：./run.sh prod"
    )
