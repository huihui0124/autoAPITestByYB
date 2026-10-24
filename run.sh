#!/usr/bin/env bash
# ============================================================================
# 咪咕票务「上线验证」回归 —— 一键入口
#
# 用法：
#   ./run.sh                # 全量回归（默认跳过需要真实资金的用例），跑完生成 Allure 报告
#   ./run.sh smoke          # 只跑冒烟：环境通不通、登录态在不在（秒级）
#   ./run.sh chain          # 只跑核心链路：商品→下单→支付记录→支付结果
#   ./run.sh b              # 只跑核销链路：C端出票→B端核销→订单已完成
#   ./run.sh refund         # 只跑退款链路：可退性→试算→提交→审批→订单已退款（★会真退一笔）
#   ./run.sh release        # 第一版上线核心回归包（release_core marker）
#   ./run.sh realpay        # 真实支付闭环：出二维码，人工扫 0.01 元（会一直等你付）
#   ./run.sh reg930         # ★930 回归用例（源表 15 行 + 补充用例 2 条），生产 888 店执行
#   MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 ./run.sh reg930   # ★生产 888 实跑
#   MIGU_ENV=prod MIGU_B_ENV=prod MIGU_ALLOW_PROD_WRITE=1 ./run.sh reg930 --real-pay=on
#                           # ★ 额外跑 TC-020「C端条件退真金闭环」：出二维码，人工扫 0.01 元
#                              （不传 --real-pay 时 TC-020 自动跳过，不产生资金流水）
#   ./run.sh all --real-pay=on     # ★ 全量 + 打开真实支付闸门：需要花真钱的用例也跑
#   ./run.sh all --real-pay=auto   # 只用"已经付过款"的订单，不花新钱
#   ./run.sh prod           # 切生产环境只跑冒烟（需先填好 config.py 的 PROD profile）
#   ./run.sh all --open     # 跑完自动打开 Allure 报告
#   ./run.sh ticket <动作>   # B 端会话（票据）管理：status|renew|harvest|paste|keepalive|schedule|cas
#   ./run.sh catalog <动作>  # ★测试专用商品清单（C 端下单白名单）：list|show|find|check|drift|add
#   ./run.sh onsale <动作>   # ★商品上架/下架（建好项目后必须上架，否则 C 端报「商品已下架」）：list|on|off
#   ./run.sh casegen <动作>  # ★测试用例生产：template|build|plan|provision|fmt-diff|fmt-scan|assets|refs
#                              PRD/技术文档 → 规范用例 Excel；缺的项目自动在 B 端建好
#                              存量资产只认「含结果」版（user 定的权威源）
#   ./run.sh report          # 汇总最近一次 Allure 结果：通过/失败/跳过/慢用例
#
# 无人值守（CI）：用 ci/run_ci.sh <test|prod> "<markers>"，它会自己设好下面这些开关；
# 直接调 pytest 也可以，但要自己带上 MIGU_CI=1 才能拿到"跳过花钱用例"的保护。
#
# 可选环境变量：
#   MIGU_CATALOG_ENFORCE=0    临时关掉「下单白名单」拦截（只做排查，正常回归别关）
#   MIGU_CATALOG_FILE=<路径>   指定别的清单文件（默认 ./catalog.json）
#   MIGU_B_TICKET='<4A票据>'  B 端会话过期时，用这张指定的票据重跑核销/退款链路
#   MIGU_C_HMCER='<hmcer>'    ★CI 用：C 端登录态直接注入（免浏览器/免短信），
#                             可给值、整段 Cookie 或 hmcer.json 路径；失效不自动续期
#   MIGU_CI=1                 无人值守模式：跳过 refund/real_pay，并禁止 --real-pay=on
#   MIGU_B_NO_AUTO_RENEW=1    关掉 B 端会话的自动恢复（只想看状态、不动浏览器数据时用）
#   MIGU_ENV=prod             切生产环境（默认 test）
#   MIGU_B_ENV=prod           切 B 端到生产（与 MIGU_ENV 各自独立！）
#   MIGU_ALLOW_PROD_WRITE=1   生产环境写操作闸门（默认关；CI 里永远不要开）
#   MIGU_REAL_PAY=1|auto|0    真实支付闸门（等价于 --real-pay=on|auto|off，命令行优先）
#   MIGU_PAY_CHANNEL=WX|AP    覆盖默认支付渠道（默认取环境配置：测试 WX / 生产 AP）
#   MIGU_REFUND_ORDER=<id>    指定要退款的订单（退款用例默认自动找一笔可退订单）
#   MIGU_REFUND_WHAT=both|ticket|money   退款范围（默认 both = 退票+退款）
#   MIGU_REFUND_PARTIAL_ORDER=<id>  指定部分退用哪笔多份单（给就不自动找/自动下单）
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

PY="./.venv/bin/python"
RESULTS="allure-results"
REPORT="allure-report"
GROUP="all"
OPEN=0
NEED_S=0        # 真实支付要 -s（二维码路径打在终端，且要留时间给你扫码）

# ---------- 快捷通道：B 端会话（票据）管理 ----------
#   ./run.sh ticket status            看会话还在不在
#   ./run.sh ticket renew             ★会话失效时从浏览器历史捞票自动换回（无需授权）
#   ./run.sh ticket renew --list      只看捞到哪些候选票据
#   ./run.sh ticket harvest           从浏览器 Cookie 库取会话（需一次钥匙串授权）
#   ./run.sh ticket paste '<URL>'     手工粘贴 4A 入口 URL / hmer
#   ./run.sh ticket keepalive         心跳保活（放定时任务）
#   ./run.sh ticket schedule          打印定时保活配置
#   ./run.sh ticket cas               打印 4A 自动登录的调研结论
if [[ "${1:-}" == "ticket" ]]; then
  shift
  if [[ ! -x "$PY" ]]; then echo "✗ 找不到虚拟环境 $PY" >&2; exit 1; fi
  exec "$PY" -m migu_api.bticket "$@"
fi

# ---------- 快捷通道：C 端登录态 ----------
#   ./run.sh login --harvest          ★ 从浏览器取登录态（免短信，推荐）
#   ./run.sh login --status           看当前登录态缓存
#   ./run.sh login --send-only        只发短信验证码
#   ./run.sh login --code 123456      用已知验证码登录
if [[ "${1:-}" == "login" ]]; then
  shift
  if [[ ! -x "$PY" ]]; then echo "✗ 找不到虚拟环境 $PY" >&2; exit 1; fi
  exec "$PY" -m migu_api.c2login "$@"
fi

# ---------- 快捷通道：Allure 结果摘要 ----------
#   ./run.sh report                    看最近一次 allure-results / allure-report 摘要
#   ./run.sh report --slow 20          多看一些慢用例
if [[ "${1:-}" == "report" ]]; then
  shift
  if [[ ! -x "$PY" ]]; then echo "✗ 找不到虚拟环境 $PY" >&2; exit 1; fi
  exec "$PY" tools/allure_summary.py "$@"
fi

# ---------- 快捷通道：测试专用商品清单（C 端下单白名单） ----------
#   ./run.sh catalog list                     看清单（哪些项目/场次/票档能下单）
#   ./run.sh catalog find --usage paid        按用途查找目标
#   ./run.sh catalog show P01                 看某个项目的完整登记
#   ./run.sh catalog check --online           自检 + 联网核对 SKU 还能不能下单
#   ./run.sh catalog drift --env all          漂移巡检：清单 vs 环境实况（只读，CI 每日跑）
#   ./run.sh catalog drift --all --deep       再查 B 端场次/选座/实名配置（需 B 端会话）
#   ./run.sh catalog add --env test --key P08 ...  追加/更新一个项目
if [[ "${1:-}" == "catalog" ]]; then
  shift
  if [[ ! -x "$PY" ]]; then echo "✗ 找不到虚拟环境 $PY" >&2; exit 1; fi
  exec "$PY" tools/catalog_ctl.py "$@"
fi

# ---------- 快捷通道：商品上架/下架（B 端） ----------
# 建好项目 ≠ C 端能下单：没上架时下单会报「商品已下架」（核心链路会成片失败）。
#   ./run.sh onsale list                      看哪些项目还没上架
#   ./run.sh onsale on --all                  ★把清单里全部项目上架
#   ./run.sh onsale on --key P08              只上架一个
#   ./run.sh onsale off --key P08             下架
#   ./run.sh onsale on --env prod --all --yes 生产上架（写操作，需显式确认）
if [[ "${1:-}" == "onsale" ]]; then
  shift
  if [[ ! -x "$PY" ]]; then echo "✗ 找不到虚拟环境 $PY" >&2; exit 1; fi
  exec "$PY" tools/onsale_ctl.py "$@"
fi

# ---------- 快捷通道：测试用例生产（PRD/技术文档 → 规范用例 + 项目落地） ----------
#   ./run.sh casegen template                        打印模版格式规范（唯一格式权威）
#   ./run.sh casegen build specs/xx.md -o out/xx.xlsx 规格 → 用例 Excel（生成即逐格校验）
#   ./run.sh casegen plan specs/xx.md                用例的「环境要求」→ 清单里的项目
#   ./run.sh casegen provision specs/xx.md           清单里缺的项目 → 在 B 端建并登记（默认 dry-run）
#   ./run.sh casegen inventory                       实际建好的商品 →《测试商品建设清单》xlsx
#                                                    （两环境实测回读，只读；-o 换输出路径）
#   ./run.sh casegen fmt-diff out/xx.xlsx            生成的 Excel 与基准模版做外观指纹比对
#   ./run.sh casegen fmt-scan <目录>                 批量体检存量用例格式（哪些跟模版分叉）
#   ./run.sh casegen assets <目录> --fails           读存量用例资产：覆盖地图 + 偏差清单
#                                                     ★默认只认「含结果」版（权威源），--all 关掉
#   ./run.sh casegen refs -q 确认码 -v              查已有用例（生成新用例前查重）
#                            -m 邀请函-权限           按功能模块查
if [[ "${1:-}" == "casegen" ]]; then
  shift
  if [[ ! -x "$PY" ]]; then echo "✗ 找不到虚拟环境 $PY" >&2; exit 1; fi
  PYTHONPATH=. exec "$PY" -m tools.casegen.cli "$@"
fi

for arg in "$@"; do
  case "$arg" in
    smoke)        GROUP="smoke" ;;
    chain)        GROUP="chain" ;;
    b|b_verify)   GROUP="b_verify" ;;
    refund|b_refund) GROUP="refund" ;;
    release|release_core) GROUP="release_core" ;;
    reg930|930)   GROUP="reg930" ;;   # 930 回归用例（回归用例.xlsx）
    realpay)      GROUP="real_pay"; export MIGU_REAL_PAY=on; NEED_S=1 ;;
    # 真实支付闸门：决定「需要花真钱/真人扫码」的用例跑不跑（on|auto|off）
    #   ./run.sh all --real-pay=on    全量 + 允许下新单等你扫码
    #   ./run.sh all --real-pay=auto  只用已付订单，不花新钱
    --real-pay)   export MIGU_REAL_PAY=on;  NEED_S=1 ;;
    --real-pay=*) export MIGU_REAL_PAY="${arg#--real-pay=}"; [[ "${MIGU_REAL_PAY}" == "on" ]] && NEED_S=1 ;;
    prod)         GROUP="smoke"; export MIGU_ENV=prod; export MIGU_B_ENV=prod ;;
    all)          GROUP="all" ;;
    --open|-o)    OPEN=1 ;;
    -h|--help)    sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "未知参数：${arg}（可用：smoke | chain | b | refund | release | realpay | reg930 | prod | all | --real-pay[=on|auto|off] | ticket | --open）" >&2; exit 2 ;;
  esac
done

# ---------- 前置检查 ----------
if [[ ! -x "$PY" ]]; then
  echo "✗ 找不到虚拟环境 $PY —— 先在该目录执行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# ---------- 环境可达性预检（快速失败） ----------
# 踩过的坑：测试环境后端挂掉时，每个用例各等 20~60s 超时 + 轮询重试，
# 全量回归能"挂"十几分钟还看不出原因。所以跑之前先用 8s 快探一轮，
# 只看"有没有 HTTP 响应"（404/302/401 都算通），超时/连接失败才算不通。
PROBE_C=0; PROBE_B=0
case "$GROUP" in
  smoke|chain|real_pay)  PROBE_C=1 ;;
  b_verify|refund)       PROBE_B=1 ;;
  release_core|reg930)   PROBE_C=1; PROBE_B=1 ;;
  all)                   PROBE_C=1; PROBE_B=1 ;;
esac
if [[ $PROBE_C -eq 1 || $PROBE_B -eq 1 ]]; then
  echo "▶ 预检环境可达性（${MIGU_ENV:-test}，8s 超时）…"
  if ! PROBE_C=$PROBE_C PROBE_B=$PROBE_B "$PY" - <<'PYEOF'
import os, sys, requests
from migu_api.config import env, b_env

# 探针必须沿用环境自己的证书策略：测试环境是「本机 nginx + 自签证书 CN=localhost」
# 转发到真实测试环境，所以 TEST profile 里 verify_ssl=False。
# 这里若用 requests 默认的 verify=True，会把「环境好好的」误报成 SSLError（踩过）。
try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

fails = []
if os.environ.get("PROBE_C") == "1":
    try:
        r = requests.get(env.base_url, timeout=8, verify=env.verify_ssl)
        print(f"  ✓ C 端 {env.base_url} → HTTP {r.status_code}")
    except Exception as e:
        print(f"  ✗ C 端 {env.base_url} → {type(e).__name__}: {str(e)[:80]}")
        fails.append("C 端")
if os.environ.get("PROBE_B") == "1":
    try:
        r = requests.get(b_env.base_url, timeout=8, verify=getattr(b_env, "verify_ssl", True))
        print(f"  ✓ B 端 {b_env.base_url} → HTTP {r.status_code}")
    except Exception as e:
        print(f"  ✗ B 端 {b_env.base_url} → {type(e).__name__}: {str(e)[:80]}")
        fails.append("B 端")
if fails:
    print(f"\n✗ {'/'.join(fails)}当前不可达 —— 不是脚本问题，逐用例超时只会白白挂住十几分钟。",
          file=sys.stderr)
    print("  请先确认：VPN 是否连接、测试环境服务是否正常（可让同事浏览器开一下后台）。",
          file=sys.stderr)
    print("  环境恢复后重跑即可；确认要硬跑可加 FORCE=1。", file=sys.stderr)
    sys.exit(4)
PYEOF
  then
    if [[ "${FORCE:-0}" != "1" ]]; then
      exit 4
    fi
    echo "⚠️  FORCE=1 —— 已按要求跳过预检结果，继续跑（预期会较慢）。"
  fi
  echo
fi

if [[ "$GROUP" == "b_verify" || "$GROUP" == "refund" || "$GROUP" == "release_core" || "$GROUP" == "all" ]]; then
  # 会话按环境分目录：.migu_state/<env>/b_cookies.json
  # 这里用 bticket ensure 做"真探活 + 自动恢复"（不只是看文件在不在）：
  #   - 有 MIGU_B_TICKET 会先换会话
  #   - 会话有效 → 直接跑核销/退款
  #   - 会话失效 → 自动依次尝试 renew（浏览器历史捞票，无需授权）/ harvest
  #   - 全部失败才打印恢复指引，核销/退款用例自动跳过（不会报错中断）
  if ! "$PY" -m migu_api.bticket ensure --quiet; then
    echo "⚠️  B 端会话不可用且自动恢复失败 —— 本轮核销/退款用例会被自动跳过。"
    echo "    人工恢复（任选其一）："
    echo "      1) 浏览器从 4A 门户进一次 B 端后台，然后：$PY -m migu_api.bticket renew"
    echo "      2) $PY -m migu_api.bticket paste '<4A 入口 URL>'"
    echo "      3) $PY -m migu_api.bticket status            # 查看状态与详细指引"
    echo "    想让它长期不用管：$PY -m migu_api.bticket schedule"
    echo
  fi
fi

# ---------- 生产环境写操作闸门 ----------
# 生产上「核心链路」「核销链路」「退款链路」会真的下单 / 真的核销 / 真的退钱，
# 产生真实业务数据与资金流水。所以默认一律拦掉，必须显式确认才放行。
if [[ "${MIGU_ENV:-test}" == "prod" && "$GROUP" != "smoke" ]]; then
  if [[ "${MIGU_ALLOW_PROD_WRITE:-0}" != "1" ]]; then
    cat >&2 <<'EOF'
✗ 已拦截：生产环境的写操作用例默认禁止执行。

  这些用例会在生产产生真实数据：
    chain    → 真的创建订单
    b_verify → 真的核销券码
    refund   → 真的发起退款、真的退钱（资金流水 + 订单变已退款）
    release  → 上线核心包，包含下单 / 核销 / 退款
    realpay  → 真的创建支付单并等你扫码付钱
    --real-pay=on → 允许用例自己下新单并等扫码（部分退需要一笔付费多份单）
    all      → 以上全部

  如果确认要在「生产测试店 storeId=888」做写操作验证，请显式放行：
    MIGU_ALLOW_PROD_WRITE=1 MIGU_ENV=prod MIGU_B_ENV=prod ./run.sh refund

  只做连通性自检（只读，安全）请用：
    ./run.sh prod
EOF
    exit 3
  fi
  echo "⚠️  生产写操作已放行（目标店铺 storeId=888 咪咕生产测试店）—— 将产生真实订单/核销/退款记录"
  echo
fi

# ---------- 清掉上一轮结果，避免新旧混在一起 ----------
rm -rf "$RESULTS" "$REPORT"

echo "▶ 环境 MIGU_ENV=${MIGU_ENV:-test} / MIGU_B_ENV=${MIGU_B_ENV:-test}｜分组 $GROUP"
# 真实支付闸门状态必须打在开头：这一行决定了"这轮要不要花钱、要不要人守着扫码"
echo "▶ 真实支付闸门：MIGU_REAL_PAY=${MIGU_REAL_PAY:-off}（off=跳过需花钱的用例；auto=只用已付订单；on=下新单并等扫码）"
echo

PYTEST_ARGS=()
[[ "$GROUP" != "all" ]] && PYTEST_ARGS=(-m "$GROUP")
# reg930：额外把 junit 结果写到 reports/reg930/，报告脚本要靠它把
# 「用例编号 → 通过/失败」映射回《回归用例.xlsx》（Allure 结果里没有用例编号）
if [[ "$GROUP" == "reg930" ]]; then
  mkdir -p reports/reg930
  PYTEST_ARGS+=(--junitxml=reports/reg930/junit.xml --alluredir=reports/reg930/allure-final)
fi
# 真实支付必须加 -s：二维码路径打在终端，而且要留时间给你扫码
if [[ "$NEED_S" == "1" ]]; then PYTEST_ARGS+=(-s); fi

set +e
# 注意：macOS 自带 bash 3.2 在 set -u 下，空数组直接展开会报 unbound variable，
# 所以这里用 ${arr[@]+"${arr[@]}"} 的写法做兼容。
# -rs 把"跳过原因"也打出来 —— 跳过通常意味着需要人工准备（如生产登录态），
# 不显示原因的话容易被当成"全绿"而忽略。
"$PY" -m pytest ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"} -q -rs
code=$?
set -e

echo
if [[ ! -d "$RESULTS" || -z "$(ls -A "$RESULTS" 2>/dev/null || true)" ]]; then
  echo "✗ 没有产生 Allure 结果，跳过报告生成。"
  exit $code
fi

echo "▶ 生成 Allure 报告…"
allure generate "$RESULTS" -o "$REPORT" --clean > /dev/null
echo "✓ 报告：$(pwd)/$REPORT/index.html"

# reg930：额外把结果回填成《回归用例（含结果）.xlsx》+ Markdown 执行报告
if [[ "$GROUP" == "reg930" ]]; then
  echo "▶ 回填用例结果并生成 930 执行报告…"
  "$PY" tools/reg930_report.py || echo "⚠ 报告回填失败（不影响测试结论，见上方报错）"
fi

if [[ $OPEN -eq 1 ]]; then
  allure open "$REPORT"
fi

if [[ $code -eq 0 ]]; then
  echo "✓ 回归通过"
else
  # 注意：变量名要用 ${code} 花括号包住。macOS 的 bash 3.2 会把紧跟其后的
  # 全角字符当成变量名的一部分，写成 $code）会报 "code：unbound variable"。
  echo "✗ 回归失败（退出码 ${code}）—— 打开上面的报告看失败步骤与截图附件"
fi
exit $code
