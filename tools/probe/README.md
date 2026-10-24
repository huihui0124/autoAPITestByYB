# tools/probe —— 一次性排查脚本（2026-09-29）

这些脚本是排查「生产部分退把整单结清」bug、以及梳理「测试商品该建在哪些层级」时，
为了摸清接口契约临时写的探针，**不是回归套件的一部分**，`run.sh` / pytest 都不会跑它们。

留在这里是为了留个"当时怎么查出来的"线索；如果确认不再需要，整个目录删掉即可，不影响任何用例。

| 脚本 | 用途 |
|---|---|
| `_probe_partial_schema.py` | 探 `refundItems` 的字段结构（份数、金额字段到底叫什么） |
| `_probe_partial.py` | 第一版部分退试探：提交只退 1 份，看订单状态怎么变 |
| `_probe_partial_ab.py` | A/B 对照：同一订单退 1 份 vs 退 2 份的表现差异 |
| `_probe_partial_calc.py` | 探试算接口（`/refund/calculate`）对份数入参的返回口径 |
| `_probe_partial_calc2.py` | 试算口径第二轮：验证"实退金额 = 单价 × 份数"是否成立 |
| `_probe_partial_calc3.py` | 试算口径第三轮：优惠分摊对部分退金额的影响 |
| `_probe_partial_index.py` | 探退款相关的接口索引（有哪些可用端点） |
| `_verify_partial.py` | 复跑验证：确认 bug 稳定复现、排除偶发 |

## 商品维度 / 建站层级探测（2026-09-29 新增）

用来梳理「项目 / 场次 / 票档」三层该建哪些测试商品。结论落在
`~/Documents/haomai/咪咕票务测试商品建设清单.xlsx`，生成脚本在 `~/Documents/haomai/.咪咕票务测试商品建设清单.ref/build.py`。

| 脚本 | 用途 |
|---|---|
| `dump_core_models.py` | 展开三层核心请求模型全部字段（必填 / 枚举 / 单值还是数组） |
| `dump_product_levels.py` | 读真实商品的项目级配置，横向对比多个商品的维度字段 |
| `dump_product_levels2.py` | 读真实商品的场次 / 票档结构；统计 `needRealNameInfo` 取值分布 |
| `probe_realname.py` | 确认实名档位取值（0 非实名 / 1 一票一证 / 2 一单一证） |
| `probe_tickets.py` | 探票档列表接口 `GET /v2/shop/product/{id}/event/ticket` |
| `enrich_yb.py` | 批量为现有 yb 商品补全实测配置（场次数 / 票档数 / 价格档），落 `yb_products.json` |
| `dump_build_levels.py` / `dump_field_levels.py` / `dump_model_endpoints.py` | 更早的三层字段归属试探（已被 `dump_core_models.py` 覆盖，保留备查） |

## 怎么跑

脚本里 `from migu_api import ...` 依赖项目根在 `sys.path` 上，所以要在**项目根目录**执行，
并把项目根加进 `PYTHONPATH`：

```bash
cd ~/Documents/haomai/allure-demo
MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 \
  PYTHONPATH=. .venv/bin/python tools/probe/_verify_partial.py
```

⚠️ 生产环境下这些脚本会真的下单/退款，**只在需要复现缺陷时**才用，且只碰 `storeId=888`。
