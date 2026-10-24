# 咪咕票务「上线验证」回归（allure-demo）

一套跑在**真实环境**上的端到端验证：C 端 H5 登录 → 下单 → 支付 → 出票，B 端核销台
核销 → 订单已完成，外加退款闭环、多次核销、部分退等专项。产出 pytest + Allure 可视化报告。

- 支持双环境：`test`（storeId=1）/ `prod`（storeId=888「咪咕生产测试店」，**只允许碰这个店**）
- C 端下单受**「测试专用商品清单」白名单**约束，只能落在登记过的项目·场次·票档上
- 花钱/真退款的用例由**真实支付闸门**控制，默认跳过

---

## 1. 目录地图

| 路径 | 作用 |
|---|---|
| `run.sh` | 本地一键入口（分组跑、生成报告、清单管理、登录态/票据管理） |
| `ci/run_ci.sh` | **CI 唯一入口**（清单体检 → 漂移巡检 → 回归 → 报告） |
| `catalog.json` | **测试专用商品清单**（C 端下单白名单），test / prod 两段 |
| `migu_api/` | 框架：`config`(环境) `passport`/`c2login`(C 端登录态) `bticket`/`badmin`(B 端) `catalog`(白名单) `client` `fundgate`(支付闸门) |
| `test_*.py` | 用例，按 marker 分组：`smoke` `catalog` `chain` `b_verify` `refund` `real_pay` |
| `tools/catalog_ctl.py` | `run.sh catalog` 的实现（list/show/find/check/drift/add） |
| `tools/onsale_ctl.py` | `run.sh onsale` 的实现（商品上架/下架）——**建好项目后必须上架**，否则 C 端报「商品已下架」 |
| `tools/probe/` | **一次性**环境建设脚本（建项目、克隆座位图）+ 环境快照数据 |
| `登录态与票据自动维持.md` | C/B 端登录态的保活细节与排障 |

---

## 2. 本地快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install allure                      # 生成可视化报告用

./run.sh smoke                           # 秒级：环境通不通、登录态在不在
./run.sh chain                           # 核心链路：商品→下单→支付记录→支付结果
./run.sh b                               # 核销链路：C 端出票 → B 端核销 → 订单已完成
./run.sh                                 # 全量（默认跳过要花真钱的用例）+ Allure 报告
./run.sh all --open                      # 跑完自动打开报告
```

登录态不在时（跑前 `./run.sh smoke` 会告诉你）：

```bash
./run.sh login --harvest                 # C 端：从浏览器取登录态（免短信，推荐）
./run.sh ticket renew                    # B 端：会话失效时从浏览器历史捞票自动换回
```

---

## 3. 测试专用商品清单（增量维护的核心）

**规则**：C 端下单只能落在 `catalog.json` 里登记过的「项目 → 场次 → 票档」上。
`client.create_order()` 下单前会自动比对，清单外直接抛 `CatalogViolation`，不发请求。

```bash
./run.sh catalog list                    # 看当前环境清单（哪些能下单）
./run.sh catalog list --env prod
./run.sh catalog show P08                # 看某个项目的完整登记
./run.sh catalog find --usage paid       # 按用途找：free_fixture / paid / partial_refund / multi_verify …
./run.sh catalog find --seat --real-name 1
```

### 3.1 新增一个项目（B 端建好之后）

```bash
./run.sh catalog add --env test --key P12 \
    --name "非选座-非实名-电子票-yb" --product-id 222999 \
    --usage seat --usage idcard \
    --event-key regular --event-id 7905001 \
    --sku 10650001:0.01:VVIP --sku 10650002:1.00:VIP
```
已存在同 key 时加 `--update` 覆盖。也可以直接编辑 `catalog.json`（格式见 `migu_api/catalog.py` 顶部注释）。

### 3.2 日常巡检（CI 每天跑，本地也能随时跑）

```bash
./run.sh catalog check                   # 离线自检：结构 + 与 config 派生值是否一致
./run.sh catalog check --online          # 再联网核对登记的 SKU 还能不能下单
./run.sh catalog drift                   # ★ 漂移巡检：清单 vs 环境实况（只读，不需要登录态）
./run.sh catalog drift --env all         # 两端都查
./run.sh catalog drift --all --deep      # 再查 B 端：场次还在不在、选座/实名配置是否被改（需 B 端会话）
```

`drift` 会告诉你三种情况，处理方式不同：
- ✗ **skuId 不存在 / 已过期** → 环境上不能下单了，去 B 端确认后 `catalog add --update` 更新清单
- ✗ **价格漂移 / 场次不存在 / 选座配置被改** → 有人在 B 端动了配置，核对后更新清单
- ! **库存为 0** → 只是售罄，B 端补库存即可，清单不用改
- · **环境上有未登记的票档** → 只是提示；要纳入下单范围才 `add --update`

> 建议做成定时任务（每天早上一封"漂移报告"），B 端配置被人手改能第一时间发现。

---

## 4. 环境与凭据

| 项 | test | prod |
|---|---|---|
| C 端站点 / storeId | union-passport.migu.cn / 1 | ticket.miguvideo.com / **888** |
| C 端登录方式 | 通行证会话 → ST → hmcer（`login --harvest` 免短信） | 手机号+真实短信，或浏览器登录态 `login --harvest` |
| B 端入口 / 接口 | ticket-test…:18084 / `/api` | ticket.miguvideo.com/bfront / `/b/api` |
| B 端鉴权 | `?iamcaspticket=xxx` 换会话（HMCER Cookie + `hm-store-id` 头） | 同左，票据来自 4A 门户（内网，需 VPN） |
| 核销次数字段 | 已上线（票带 totalVerifyCount / verifyRecordList） | **已上线**（2026-09-30 复测；票据与核销记录接口都有数据） |
| 多次核销**配置**（场次 maxVerifyCount） | 已上线（P01 多次核销场次=3） | **未上线**（`events/edit` 无该字段 → 所有场次恒 1 次） |
| 其它 | — | 年龄限购接口 404；0 元夹具单不能用于部分退（会假绿） |

**环境开关**：`MIGU_ENV` / `MIGU_B_ENV`（C 端 / B 端，各自独立，都必须显式指定 prod 才动生产）。
凭据文件按环境隔离在 `.migu_state/<env>/`，**不进版本库**。

---

## 5. CI 接入

### 5.1 一条命令

```bash
ci/run_ci.sh                 # 测试环境：check + drift + smoke/chain
ci/run_ci.sh prod            # 生产环境：check + drift + smoke（只读）
ci/run_ci.sh test "smoke or chain or b_verify"
```

脚本内部固定：`MIGU_CI=1`、`MIGU_B_NO_AUTO_RENEW=1`，并 unset `MIGU_ALLOW_PROD_WRITE`
与 `MIGU_CATALOG_ENFORCE`（白名单闸门必须开着）。

### 5.2 三道安全闸门（CI 不可能误花钱）

| 闸门 | 位置 | 效果 |
|---|---|---|
| `MIGU_CI=1` | `conftest.py` | 收集期跳过 `refund` / `real_pay`；`--real-pay=on` 直接**报错退出** |
| `MIGU_ALLOW_PROD_WRITE` | `config.py` + 各用例 | 生产环境的写过例默认全拦，只有显式设 1 才放行 |
| 白名单 `MIGU_CATALOG_ENFORCE` | `catalog.py` | 清单外的商品/票档下不了单 |

### 5.3 需要的 Secret

| Secret | 内容 | 怎么取 | 失效表现 |
|---|---|---|---|
| `MIGU_C_HMCER` | C 端业务登录态 | `./run.sh login --harvest` 后取 `.migu_state/<env>/hmcer.json` 里的 `hmcer` | 接口 401，报错会提示"注入的登录态已失效，请更新 Secret" |
| `MIGU_B_TICKET` | B 端 4A 票据（`?iamcaspticket=` 后面那段） | 走 4A 门户点进赛事票务管理平台，复制 URL 参数 | B 端 `ssoCallback` 失败 |

`MIGU_C_HMCER` 支持三种写法（任选能塞进 Secret 的）：直接给值 / 整段 Cookie 字符串
（自动挑 `hmcer=xxx`）/ 指向一份 `hmcer.json` 的路径。

### 5.4 流水线模板

- GitHub Actions：`.github/workflows/regression.yml`（手动触发 + 每天 09:30 定时 + push 触发，跑完上传 Allure 结果与报告）
- GitLab CI / Jenkins：直接调 `ci/run_ci.sh <env> "<markers>"`，把 `MIGU_C_HMCER` / `MIGU_B_TICKET`
  配成 Masked Variable / Credential，其余步骤（装依赖、上传产物）按平台写即可

### 5.5 票据轮换 SOP（CI 上唯一需要人工的事）

1. 本地 `./run.sh ticket status` / `./run.sh login --status` 看还剩多久
2. 快过期时：本地 `./run.sh login --harvest`（C 端）、`./run.sh ticket renew`（B 端）各取一份新值
3. 更新 CI Secret，重跑一次流水线确认变绿
4. 建议：把 `./run.sh ticket keepalive` 挂本地定时任务（cron / launchd），能显著拉长 B 端票据寿命

---

## 6. 已知坑（省得再踩一遍）

- **建好项目 ≠ C 端能下单**：B 端建完项目后还差**商品上架**这一步（UI 里是「保存并上架」 /
  列表行的「上架」按钮）。没上架时 C 端 `makeOrder` 报 **`商品已下架`（code=400）**，
  核心链路会成片失败，看着像框架坏了。用 `./run.sh onsale list` 查、`./run.sh onsale on --all` 修。
  注意 `tools/probe/build_prod_*.py` 建项目**不会自动上架**。
- **B 端回读怪癖**：选座项目 `GET /v1/seat/event/{evid}` 对**非首个场次**可能不返回
  `productSkuList`，但座位-sku 绑定与 C 端可见性都正常，不影响用例。
- **座位图保存**：`/v1/seat/resolve/excel` 直连 API 恒 500（前端复制流程也不调它）；
  保存时必须带上每个座位的 `ticket:{priceName,price}`，模板 URL 要用后端可达的
  `sspw-hdsz2.eos-suzhou-4.cmecloud.cn` 域名。
- **「多次核销」分两层，别再一句话下结论**（2026-09-30 更正）：
  ① **票据的核销次数字段/核销记录接口生产已上线**（2026-09-29 记的"生产不返回次数字段、
  核销记录恒 []"已过期）；② 但**场次级 `maxVerifyCount` 配置生产没发布**
  （B 端 `events/edit` 无该字段、`events/list` 不返回），所以生产所有票恒 1 次。
  ⇒ 核销用例的断言口径改成**运行时探测**（票据有没有 `totalVerifyCount`），
  不再看静态开关 `env.supports_multi_verify` —— 开关过期会造成假红。
- **核销记录接口是"按券码聚合"的一条**（`/v1/verification/voucher`），
  逐次流水在票据的 `verifyRecordList`（verifySeq 1..N）。别拿"记录条数 == 核销次数"当断言。
- **生产缺口**：年龄限购接口 404（配置不了 → P10/P11 的"18+可购场次"在生产其实不限龄）；
  「多次核销」的配置项未发布。相关用例会自动 **skip 并写明原因**，不要当失败。
- **选座项目的票档配置会漏**：`seat/save` 走的是 `ticketPriceList`（只带
  `name/salePrice/skuAddition.bindId`），**不携带 `fixedSaleTime`** —— 于是克隆到生产的
  选座项目丢掉了开售时间窗（实测生产 P10「未开售场次」5 个票档 `fixedSaleTime` 全为 None，
  等于已开售）。`./run.sh catalog drift` 会报「未开售时间窗缺失」，修它需要在 B 端
  给该场次票档补一个未来的开售时间。
- **生产部分退**：只能用真实付费多份单，0 元夹具单会假绿（`supports_free_multi_fixture=False`）。
- **票务环境写操作**：生产永远只碰 storeId=888。

---

## 7. 版本管理

仓库需在**你自己的终端**里初始化一次（本项目的运行环境不允许代理进程操作 `.git` 锁文件）：

```bash
cd ~/Documents/haomai/allure-demo
git init -b main
git add -A && git commit -m "chore: 回归框架基线（清单/catalog + CI 闸门）"
git remote add origin <你的仓库地址> && git push -u origin main
```

`.gitignore` 已经写好：**凭据（`.migu_state/`）、Allure 产物、大体积快照**都不会入库，
而 `catalog.json`、swagger 快照、构建脚本会入库（它们是"环境里到底有什么"的记录）。
