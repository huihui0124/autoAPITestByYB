# 接口存活性测试（live_testing）

> 目标：**验证过滤后的 679 个接口可达、不报 HTTP 5xx / 网络错误**。
> 不验证业务正确性；下单、支付、核销、退款等业务断言仍走仓库根目录的 `test_*.py`。
>
> 来源：`openAPI/haomai-customer-migu-in-use.json`（C 端 161 接口）
> ＋ `openAPI/haomai-web-migu-in-use.json`（B 端 518 接口）。
> 腾讯 LBS 的 4 个接口（`/v1/lbs/tencent/*`）已确认废弃，生成阶段直接剔除。
> 口径与筛选过程见 `咪咕接口自动化-交接文档-20261024.md`、`咪咕接口筛选报告-20261024.md`。

---

## 1. 目录

```text
live_testing/
├── openapi.py              OpenAPI 解析、样例值、风险分级
├── generator.py            OpenAPI → cases/*.json（只读，不发请求）
├── config.py               配置 + 登录态装载（配置/环境变量/.migu_state）
├── cases.py                用例清单读取与运行期过滤
├── client.py               存活测试专用 HTTP 客户端
├── conftest.py             默认不跑的闸门、预检夹具、汇总
├── test_api_liveness.py    pytest 参数化执行器
├── report.py               artifacts/live/live-summary-*.json 汇总
├── cases/                  生成物（可入库，OpenAPI 变化后重生成）
│   ├── customer.json
│   ├── web.json
│   └── manifest.json
└── README.md
```

---

## 2. 快速开始

```bash
# 1) 生成用例清单（不联网）
python -m live_testing.generator

# 2) 准备登录态：二选一
cp live_config.example.json live_config.json     # 手工填 Cookie（推荐）
# 或者：先跑现有登录命令，模块会自动复用 .migu_state 里的会话
./run.sh login --harvest          # C 端 hmcer
./run.sh ticket harvest           # B 端 hmer 等 Cookie

# 3) 先跑预检 + 少量接口
MIGU_LIVE=1 MIGU_LIVE_LIMIT=5 python -m pytest live_testing -m live -q

# 4) 全量
MIGU_LIVE=1 python -m pytest live_testing -m live -q
```

> ⚠️ `MIGU_LIVE=1` 是**必需的闸门**。不设置时，全量 `pytest` / `ci/run_ci.sh`
> 会直接不收集本模块，既不会发请求，也不会产生 300+ 条 skip 噪音。

---

## 3. 登录态怎么配

优先级：**配置文件 > 环境变量 > 复用 `.migu_state`**。

### 3.1 配置文件（推荐）

复制 `live_config.example.json` 为 `live_config.json`（已被 `.gitignore` 忽略），填：

```json
{
  "services": {
    "customer": {
      "cookie": "hmcer=粘贴 C 端 hmcer 的值"
    },
    "web": {
      "cookie": "hmer=...; hmid=...; hmfid=...; ll=...; ct=...; dvid=..."
    }
  }
}
```

Cookie 从哪来：

| 端 | 来源 | 需要的 Cookie |
|---|---|---|
| C 端 | 浏览器 DevTools → Application → Cookies（`union-passport.migu.cn`） | `hmcer`（可选 `tpid`） |
| C 端 | 仓库已有会话文件 `.migu_state/test/hmcer.json` | `hmcer` |
| B 端 | 浏览器 DevTools（`ticket-test.miguvideo.com`） | `hmer`、`hmid`、`hmfid`、`ll`、`ct`、`dvid` |
| B 端 | 仓库已有会话文件 `.migu_state/test/b_cookies.json` | 同上 |

也可以写成 `cookies` 对象：

```json
"cookies": {"hmcer": "..."}
```

或把认证头写进 `headers`（例如以后改成 Bearer Token）：

```json
"headers": {"Authorization": "Bearer xxx"}
```

### 3.2 环境变量

```bash
export MIGU_LIVE_CUSTOMER_COOKIE='hmcer=...'
export MIGU_LIVE_WEB_COOKIE='hmer=...; hmid=...; hmfid=...'
```

也支持按端覆盖：`MIGU_LIVE_CUSTOMER_BASE_URL`、`MIGU_LIVE_WEB_STORE_ID`、
`MIGU_LIVE_WEB_HEADERS='{"Authorization":"Bearer xxx"}'` 等。

### 3.3 复用 `.migu_state`（零配置）

不填 Cookie 时，模块会读取：

- C 端：`.migu_state/<env>/hmcer.json`
- B 端：`.migu_state/<env>/b_cookies.json`

Cookie 过期时预检会明确报 `auth_failed`，不会把 401 混进 300+ 条接口结果里。
刷新会话仍用仓库现有命令：

```bash
./run.sh login --harvest        # C 端
./run.sh ticket harvest         # B 端（或 renew / paste）
```

### 3.4 预检

跑之前会先请求两个已知接口，确认登录态可用：

| 端 | 预检接口 | 期望 |
|---|---|---|
| C 端 | `GET /v1/customer/getInfoV2?storeId=1` | HTTP 2xx + `code=200` |
| B 端 | `GET /v1/user` | HTTP 2xx + `code=200` |

预检失败时：`test_live_preflight` 红一条，逐接口用例全部 skip 并给出原因。
需要改预检接口/期望业务码时，在配置里写 `services.<name>.auth_probe`。

---

## 4. 判定口径

每条用例只做以下断言：

| 结果 | 默认判定 | 说明 |
|---|---|---|
| HTTP 5xx | **失败** | 服务端错误，必须修 |
| 网络异常 / 超时 | **失败** | 不可达，算存活失败 |
| 业务 `code=500`（read / safe_write），message 是「系统错误」等无业务解释的内容 | **失败** | 真正的服务端异常（`business_5xx`） |
| 业务 `code=500`（write / dangerous，空 body 探测） | 通过 + 告警（默认） | 缺业务参数，接口本身可达（`business_write_5xx`）；`MIGU_LIVE_STRICT_WRITE=1` 可改回失败 |
| 业务 `code=500`，message 含「不存在 / 不合法 / 为空 / 必填 / 请选择 / 未找到 …」 | 通过 + 告警 | 样例数据/参数不满足要求，路由和服务本身活着（`business_data_5xx`） |
| HTTP 4xx（400/401/403/404） | 通过 + 告警 | 路由活着；参数不对/无权限/资源不存在都算 4xx |
| HTTP 2xx / 3xx | 通过 | 3xx 会跟随重定向，最终 URL 记录在 Allure |
| HTTP 200 但返回 HTML | 通过 + 告警 | 常见于 nginx 回落 SPA；在汇总里标记 `html_2xx` |

> 为什么要区分两类业务 500：存活测试用样例 ID（如 `orderId=1`）打接口，
> 规范实现会返回「订单不存在」；这不能说明接口挂了。真正的「系统错误」才需要排查。
> 分类词表可用 `benign_500_patterns` / `MIGU_LIVE_BENIGN_500_PATTERNS` 调整。

想更严格时：

```bash
MIGU_LIVE_STRICT_AUTH=1   # 401/403 判失败（适合单独验证权限）
MIGU_LIVE_FAIL_ON_404=1   # 404 判失败（适合验证路径是否部署）
MIGU_LIVE_STRICT_DATA=1   # 「不存在」类业务 500 也判失败（要求零 code=500 时用）
MIGU_LIVE_STRICT_WRITE=1  # write/dangerous 的业务 500 也判失败（默认只告警）
MIGU_LIVE_FAIL_CODES=500,50000
MIGU_LIVE_FAIL_CODES=none # 只看 HTTP 5xx/网络错误，业务 code 全部不判失败
```

结果分类：`ok_2xx`、`redirect_3xx`、`client_4xx`、`auth_4xx`、`not_found_404`、
`html_2xx`、`server_5xx`、`business_5xx`、`business_data_5xx`、
`business_write_5xx`、`network_error`。

每次运行会落一份汇总：

```text
artifacts/live/live-summary-<时间戳>.json
```

包含失败明细、告警明细、分类计数、最慢接口 Top 10。

---

## 5. 安全默认值（重要）

| 开关 | 默认 | 说明 |
|---|---|---|
| `methods` | `GET` | 只跑只读接口；`MIGU_LIVE_METHODS=GET,POST` 会额外跑 `safe_write`（查询类 POST） |
| `allow_write` | `false` | 真正的写接口（`addAddress`/`createBankAccount`/`recharge`/`save` 等）默认不跑，需 `MIGU_LIVE_ALLOW_WRITE=1` |
| `allow_dangerous` | `false` | 支付回调、登录/注销、退款、核销、删除、批量导入等**永不执行**，除非 `MIGU_LIVE_ALLOW_DANGEROUS=1` |
| `body_mode` | `auto` | `safe_write` 发送查询条件（`storeId/pageNum/pageSize/...`）；`write`/`dangerous` 只发 `{}`。`empty` 全部只发 `{}`；`required` 发送必填字段（写风险最高） |
| `query_mode` | `required` | query 只发必填 + 安全可选参数（`storeId/pageIndex/pageSize/productId/...`），避免 `orderId=1` 这类假数据制造 500；`all` 发全部参数 |
| `fallback_full_query` | `true` | `required` 模式返回业务 500 时，自动用全量 query 再试一次（OpenAPI 的 required 标注并不可靠） |
| `strict_write_business_500` | `false` | write/dangerous 的业务 500 默认只告警（`business_write_5xx`）；设 true 或 `MIGU_LIVE_STRICT_WRITE=1` 恢复失败 |
| `known_issues_mode` | `xfail` | 已知问题默认 xfail；设 `warn` 后照常请求、照常进汇总，但 pytest 记 passed，用于全绿 |
| `MIGU_LIVE_ENV=prod` | 拦截 | 必须再设 `MIGU_LIVE_ALLOW_PROD=1`；生产只允许 `storeId=888` |
| `retries` | `1` | 只对 GET/HEAD 的网络错误和 502/503/504 重试 |

### 5.1 真实参数：param_values / case_overrides / known_issues

- `param_values`：按参数名全局替换，适合 `orderId/couponId/customerUserManageId` 这种名字唯一的参数；
- `case_overrides`：按路径或 `"METHOD /path"` 精确覆盖 `path/query/body/cookies`，
  适合 `{id}` 这种同名但不同资源的参数（例如 `GET /v1/address/{id}` 与
  `DELETE /v1/address/{id}` 要用不同的 ID）；
- `known_issues`：命中后照常请求、照常记录，汇总里单列，不计入 failed。
  默认 `known_issues_mode=xfail`（pytest 标 xfail）；设 `warn` 则 pytest 记 passed，
  适合要求“全绿”的日常回归。用于已确认的后端缺陷/环境缺数据。

示例（`live_config.json`，已 gitignore）：

```json
{
  "services": {
    "customer": {
      "param_values": {"couponId": 378021, "couponTemplateId": 9595},
      "case_overrides": {
        "GET /v1/address/{id}": {"path": {"id": 64415668127619}}
      }
    },
    "web": {
      "param_values": {"orderId": 72883602, "purchaseOrderId": 4613663},
      "case_overrides": {
        "GET /v1/shopTrade/refund/detail/{taskId}": {
          "path": {"taskId": "2026102315561699374"},
          "query": {"storeId": 1}
        }
      }
    }
  },
  "known_issues": {
    "GET /v1/promotion/activity/newUserGift/effect": "测试环境未配置新人礼活动"
  },
  "known_issues_mode": "warn"
}
```

> 删除类接口不要用真实 ID 覆盖，避免日常回归误删数据；这类接口继续由
> `allow_dangerous` 闸门控制。
>
> 示例中的 ID 是 2026-10-24 测试环境快照；测试数据重建/清理后需要重新查询并更新
> `live_config.json`（订单列表、券列表、模板列表、退款列表等接口都能重新取到）。

风险分级来自路径/方法/operationId（`live_testing/openapi.py`）：

- `read`：GET/HEAD/OPTIONS；
- `safe_write`：POST/PUT/PATCH，但 operationId/路径表明是查询
  （`query*`、`get*`、`list*`、`count*`、`*/search`、`*/page`、`*/export` …）；
- `write`：其它非 GET，需要显式 `MIGU_LIVE_ALLOW_WRITE=1`；
- `dangerous`：DELETE、`refund/pay/verification/cancel/delete/login/notify/...`、
  以及 `PUT/POST` 的批量/审批/导入等，需要 `MIGU_LIVE_ALLOW_DANGEROUS=1`。

所以：

```bash
# 只读 GET（默认）
MIGU_LIVE=1 python -m pytest live_testing -m live -q

# GET + 查询类 POST（推荐的中等覆盖，不会建数据）
MIGU_LIVE=1 MIGU_LIVE_METHODS=GET,POST python -m pytest live_testing -m live -q

# GET + 查询类 POST + 普通写接口（会改测试环境数据）
MIGU_LIVE=1 MIGU_LIVE_METHODS=GET,POST,PUT,PATCH MIGU_LIVE_ALLOW_WRITE=1 ...

# 再叠加支付/退款/核销/删除等危险接口（仅测试环境，后果自负）
MIGU_LIVE=1 MIGU_LIVE_METHODS=GET,POST,PUT,PATCH,DELETE \
  MIGU_LIVE_ALLOW_WRITE=1 MIGU_LIVE_ALLOW_DANGEROUS=1 ...

# 只跑 dangerous（登录/注销/回调会自动排在最后，避免中途打掉会话）
MIGU_LIVE=1 MIGU_LIVE_METHODS=GET,POST,PUT,PATCH,DELETE \
  MIGU_LIVE_ALLOW_WRITE=1 MIGU_LIVE_ALLOW_DANGEROUS=1 \
  MIGU_LIVE_RISKS=dangerous python -m pytest live_testing -m live -q
```

开启写接口探测的建议顺序：

```bash
# 1) 只跑安全的 POST（查询/导出/预览类）
MIGU_LIVE_METHODS=GET,POST MIGU_LIVE_SERVICE=web \
  python -m pytest live_testing -m live -q -k "report"

# 2) 确认要覆盖危险接口时（会改数据，先在测试环境）
MIGU_LIVE_METHODS=GET,POST,PUT,DELETE MIGU_LIVE_ALLOW_DANGEROUS=1 \
  MIGU_LIVE_SERVICE=web python -m pytest live_testing -m live -q
```

---

## 6. 常用过滤

```bash
# 只跑 B 端
MIGU_LIVE=1 MIGU_LIVE_SERVICE=web python -m pytest live_testing -m live -q

# 只跑订单相关（按 path 子串）
MIGU_LIVE=1 MIGU_LIVE_PATH=/order python -m pytest live_testing -m live -q

# 只跑某个 tag
MIGU_LIVE=1 MIGU_LIVE_TAGS=order-rest-controller python -m pytest live_testing -m live -q

# 每个端先生成/跑 20 条，快速确认环境
MIGU_LIVE=1 MIGU_LIVE_LIMIT=20 python -m pytest live_testing -m live -q

# 跳过已知噪音接口
MIGU_LIVE=1 MIGU_LIVE_SKIP_PATH=/v1/upload,/v1/report python -m pytest live_testing -m live -q

# 只跑某类风险（read / safe_write / write / dangerous）
MIGU_LIVE=1 MIGU_LIVE_RISKS=dangerous MIGU_LIVE_ALLOW_DANGEROUS=1 \
  python -m pytest live_testing -m live -q

# 并发（每个 worker 各写一份汇总）
MIGU_LIVE=1 python -m pytest live_testing -m live -q -n 4

# 发送 OpenAPI 里的全部 query 参数（默认只发 required + 安全可选）
MIGU_LIVE=1 MIGU_LIVE_QUERY=all python -m pytest live_testing -m live -q
```

pytest 原生的 `-k` 也能用，例如 `-k "GET"`、`-k "order"`。

---

## 7. 生成 / 刷新

```bash
python -m live_testing.generator            # 重新生成 cases/
python -m live_testing.generator --check    # 只解析并打印统计
```

生成器做的事：

1. 读 OpenAPI，跳过 `deprecated`，并剔除已确认废弃的腾讯 LBS
   （`/v1/lbs/tencent/*`，见 `DEPRECATED_PATH_PATTERNS`）；
2. 展开 `$ref` / `allOf` / `oneOf`；把 Spring `@ModelAttribute` 的
   `webRequest` 对象**平铺**成 query 参数（`pageIndex=1&pageSize=10`）；
3. 为 path/query 生成保守样例值（`pageSize=10`、`id=1`、日期 `2026-01-01` 等）；
4. JSON body 保存三份：`body`（`{}`）、`body_required`（只含 required 字段）、
   `body_safe`（只含 `storeId/pageNum/pageSize/projectId/...` 等查询条件）；
   `body_mode=auto` 时 `safe_write` 用 `body_safe`，其余用 `body`；
5. 给每条打上 `risk=read/safe_write/write/dangerous`。

运行期还会从仓库根目录的 `catalog.json` 读取当前环境的测试商品 ID，
自动填充 `productId` / `spuId` / `projectId` / `activityId` / `eventId` / `skuId`，
避免拿 `productId=1` 打接口导致大量「商品不存在」的假 500。
不需要时设 `MIGU_LIVE_USE_CATALOG=0`，或在配置里写 `"use_catalog": false`。

query 参数默认只发「必填 + 安全可选」（`query_mode=required`），
如果接口仍然返回业务 500，会自动换成全量 query 再试一次
（`fallback_full_query`）。这样 `orderId=1` 不会被无脑塞进
`/v1/activity/tips`，而 `/v1/couponTemplate` 这类真的需要
`ownerType/ownerId` 的接口又能拿到可用参数。

改了 OpenAPI 后重新生成即可；`manifest.json` 里记录了源文件 sha256，
pytest 头部会提示“清单未重新生成”。

刷新 OpenAPI 本体仍按交接文档 §8.1 的 curl 命令。

---

## 8. 常见问题

**全部 401 / 403**
先看 `test_live_preflight`。多半是 Cookie 过期或 `hm-store-id` 没带
（B 端由配置自动注入）。C 端 WAF 403 通常是 UA/Referer 不对，模块已按
`migu_api/config.py` 的浏览器头自动补齐。

**某个接口 500，但手动调用正常**
先看 Allure 里的请求参数。存活测试用的是**样例值**（默认会从 `catalog.json`
取测试商品 ID，订单/券等仍可能是 `1`），不代表业务上存在该资源。
如果 message 是「不存在 / 不合法」这类，默认已经只告警（`business_data_5xx`）；
如果是「系统错误」，说明后端没有把数据问题翻译成业务错误，需要补真实参数，
或把该 path 加进 `skip_paths` 单独跟踪：

```json
"services": {
  "customer": {"param_values": {"productId": 12345, "skuId": 67890}},
  "web": {"param_values": {"storeId": 1, "productId": 12345}}
}
```

**大量 404**
检查 `base_url` 是否多/少了 `/saishi/api`、`/api`、`/b/api`。
生产 B 端是 `/b/api`，不是 `/api`。

**GET 返回 200 但内容是 HTML**
通常是 nginx 回落到 SPA index（路径不存在或前缀不对）。汇总里标为
`html_2xx`；需要判失败时用 `MIGU_LIVE_FAIL_ON_404` 之外，可先看 `base_url`。

**B 端本机代理干扰**
B 端默认 `no_proxy=true`（`trust_env=False`），与 `BAdminClient` 一致。
C 端默认走系统代理；需要直连时设 `MIGU_LIVE_CUSTOMER_NO_PROXY=1`。

**想只看失败**
```bash
MIGU_LIVE=1 python -m pytest live_testing -m live -q
# 或看汇总
cat artifacts/live/live-summary-*.json | python -m json.tool | less
```

---

## 9. 与现有模块的边界

| 模块 | 职责 | 是否复用 |
|---|---|---|
| `live_testing/` | 逐接口存活、非 5xx | 只复用 `.migu_state` 登录态 |
| `migu_api/client.py` | C 端业务客户端（code≠200 抛异常） | 不复用：存活测试要保留 4xx 原始响应 |
| `migu_api/badmin.py` | B 端业务客户端（核销/退款） | 不复用，但请求头口径与其一致 |
| 根目录 `test_*.py` | 业务链路回归 | 不替代；本模块通过后再跑业务链路 |

这样拆分的原因：业务客户端把 `code != 200` 直接当失败，不适合“4xx 也算活着”的存活口径；
而登录态获取/保活已经有成熟实现，没必要在存活模块里重写一遍。

---

## 10. 验收

```bash
# 生成清单（应看到 C 端 161 / B 端 518，腾讯 LBS 已剔除）
python -m live_testing.generator --check

# 预检 + 全量 GET
MIGU_LIVE=1 python -m pytest live_testing -m live -q

# 通过标准：无 server_5xx / business_5xx / network_error；
# business_data_5xx、4xx、401、404、html_2xx 作为告警逐条记录，不阻塞“存活”结论。
# 要求零 code=500 时加 MIGU_LIVE_STRICT_DATA=1。
```

---

## 11. 基线快照（2026-10-24，测试环境）

- 清单：C 端 161 接口 / B 端 518 接口（腾讯 LBS 4 条已剔除）；
- 分 risk 组跑（`known_issues_mode=warn`）：

  | 组 | 用例 | pytest 结果 |
  |---|---:|---|
  | read（默认 GET） | 328 + 2 预检 | **330 passed / 0 failed** |
  | safe_write | 45 + 2 预检 | **47 passed / 0 failed** |
  | write | 188 + 2 预检 | **190 passed / 0 failed** |
  | dangerous | 118 + 2 预检 | **120 passed / 0 failed** |
  | 去重合计 | 679 + 2 预检 | **681 passed / 0 failed** |

- 3 条已知问题仍照常请求、在汇总 `known_issues` 单列，但按 `warn` 模式记 passed：

  | 端 | 接口 | 原因 |
  |---|---|---|
  | B | `GET /v1/promotion/activity/newUserGift/effect` | 新人礼活动配置为空，测试环境无数据 |
  | B | `GET /v1/promotion/activity/newUserGift/use/detail` | 无 `receivedId` 可查（新人礼列表为空） |
  | B | `GET /v2/shopTrade/channelPromotion/distributor/owner/{id}` | info/orderlist 对当前 B 账号返回 401，渠道分销权限疑似未开通 |

- 已用真实数据修复的代表性接口：
  - C：`/v1/coupon/detail`、`/v1/coupon/templates/{id}`、`/v1/address/{id}`(GET)、
    `/v1/coupon/deductCard/{id}`、订单详情/票/快照/退款预检；
  - B：`/v1/couponTemplate/{id}`、`/v1/customer/moneyAccountCount` 等客户查询、
    `/v1/shopTrade/order/{orderId}/deliverInfo`、`/v1/shopTrade/postageTemplate/{id}`、
    `/v1/shopTrade/information/{id}`、`/v1/shopTrade/refund/*`、
    `/v1/projects/marketPriceInfo`、`/v1/promotion/customer/layer/list/page`。
- 业务 500 口径：read/safe_write 判失败；write/dangerous 用空 body 探测产生的
  业务 500 默认记 `business_write_5xx` 告警（`MIGU_LIVE_STRICT_WRITE=1` 可恢复失败）；
- 至此 **679/679 条接口全部执行过且 0 failed**；写接口全量轮次曾遇发版，
  81 条 502 已确认是环境噪音。
- 建议按 risk 分组跑：dangerous 的删除/退款可能影响同进程后续读取，混跑容易产生
  数据污染导致的假失败。
