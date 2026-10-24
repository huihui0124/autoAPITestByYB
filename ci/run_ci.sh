#!/usr/bin/env bash
# ============================================================================
# ci/run_ci.sh —— 无人值守流水线的唯一入口（GitHub Actions / GitLab CI / Jenkins 都调它）
#
# 用法：
#   ci/run_ci.sh                      # 默认跑测试环境：清单体检 + 冒烟 + 核心链路
#   ci/run_ci.sh test                 # 同上
#   ci/run_ci.sh prod                 # 生产环境：**只读**（清单体检 + 漂移 + 冒烟）
#   ci/run_ci.sh test "smoke or chain or b_verify"    # 自定义 pytest marker 表达式
#
# 需要注入的凭据（走 CI Secret，别写进仓库；详见 README「CI 接入」）：
#   MIGU_C_HMCER    C 端业务登录态（hmcer）。生产必须；测试没有通行证 Cookie 时也需要
#   MIGU_B_TICKET   B 端 4A 票据。跑核销（b_verify）必须
#
# 本脚本刻意**不做**的事（CI 里永远别做）：
#   · 不跑 refund（会真退一笔钱）
#   · 不跑 real_pay（要真人扫码付款）
#   · 不打开 MIGU_ALLOW_PROD_WRITE（生产环境的写操作一律拦）
#   这几道闸门由 conftest.py（MIGU_CI=1）在**代码层**再兜一层，脚本写漏也不会出事。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_NAME="${1:-test}"
MARKERS="${2:-}"
PY="./.venv/bin/python"
RESULTS="allure-results"

case "$ENV_NAME" in
  test|prod) ;;
  *) echo "✗ 未知环境：${ENV_NAME}（可用 test / prod）" >&2; exit 2 ;;
esac

# 默认分组：测试环境跑核心链路；生产只跑只读冒烟 + 清单体检
if [[ -z "$MARKERS" ]]; then
  if [[ "$ENV_NAME" == "prod" ]]; then
    MARKERS="smoke or catalog"
  else
    MARKERS="smoke or catalog or chain"
  fi
fi

# ---------- 环境变量 ----------
export MIGU_ENV="$ENV_NAME"
export MIGU_B_ENV="$ENV_NAME"
export MIGU_CI=1                    # ★ 无人值守模式：conftest 会跳过花钱/退款的用例
export MIGU_B_NO_AUTO_RENEW=1       # CI 上没有浏览器，别去尝试从浏览器捞票
export PYTHONUNBUFFERED=1
unset MIGU_ALLOW_PROD_WRITE || true # 生产写操作一律拦（哪怕 Secret 里误设了）
unset MIGU_CATALOG_ENFORCE || true  # 白名单闸门必须开着
echo "[CI] 环境=${ENV_NAME}  分组=${MARKERS}"

# ---------- 依赖 ----------
if [[ ! -x "$PY" ]]; then
  echo "[CI] 建虚拟环境并安装依赖"
  python3 -m venv .venv
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
fi

# ---------- 凭据自检（缺了立刻失败，别等用例报 401） ----------
C_CACHE=".migu_state/$ENV_NAME/hmcer.json"
missing=0
if [[ -z "${MIGU_C_HMCER:-}" && ! -f "$C_CACHE" ]]; then
  echo "✗ 环境 $ENV_NAME 没有 C 端登录态：既没注入 MIGU_C_HMCER，" \
       "本地也没有 $C_CACHE" >&2
  echo "  本地：./run.sh login --harvest（从浏览器取）｜ CI：把 hmcer 配成 Secret" >&2
  missing=1
fi
if [[ "$MARKERS" == *b_verify* && -z "${MIGU_B_TICKET:-}" && ! -f ".migu_state/$ENV_NAME/b_cookies.json" ]]; then
  echo "✗ 要跑核销链路（b_verify）必须有 B 端会话：注入 MIGU_B_TICKET 或本地有 b_cookies.json" >&2
  missing=1
fi
[[ "$missing" == "1" ]] && exit 3

if [[ -n "${MIGU_C_HMCER:-}" ]]; then
  echo "[CI] C 端登录态：MIGU_C_HMCER 注入（只读，失效不会自动续期）"
else
  echo "[CI] C 端登录态：本地缓存 $C_CACHE"
fi
[[ -n "${MIGU_B_TICKET:-}" ]] && echo "[CI] B 端票据：MIGU_B_TICKET 注入"

rc=0

# ---------- 1) 清单体检（离线，秒级；check 自带两端自检）----------
echo; echo "=== [1/3] 清单自检（catalog check）==="
./run.sh catalog check || rc=1

# ---------- 2) 漂移巡检（只读线上，确认清单跟环境还一致）----------
echo; echo "=== [2/3] 漂移巡检（catalog drift）==="
MIGU_ENV="$ENV_NAME" ./run.sh catalog drift --env "$ENV_NAME" || rc=1

# ---------- 3) 回归 ----------
echo; echo "=== [3/3] 回归（pytest -m \"$MARKERS\"）==="
rm -rf "$RESULTS"
"$PY" -m pytest -m "$MARKERS" -p no:cacheprovider --alluredir="$RESULTS" || rc=1

# ---------- 报告（有 allure CLI 就生成，没有就留着 allure-results 让 CI 上传）----------
if command -v allure >/dev/null 2>&1 && [[ -d "$RESULTS" ]]; then
  echo; echo "=== 生成 Allure 报告 ==="
  allure generate "$RESULTS" -o "reports/allure-report-$ENV_NAME" --clean >/dev/null 2>&1 \
    && echo "[CI] 报告：reports/allure-report-$ENV_NAME/index.html" \
    || echo "[CI] 报告生成失败（不影响回归结论）"
else
  echo "[CI] 未安装 allure CLI —— 直接上传 $RESULTS 目录，或改用在线报告服务"
fi

echo
if [[ "$rc" == "0" ]]; then
  echo "[CI] ✓ 全部通过（环境 ${ENV_NAME}）"
else
  echo "[CI] ✗ 有失败项（环境 ${ENV_NAME}）—— 先看上面的 ✗ 行：清单漂移、凭据过期、真实用例失败分得很清"
fi
exit "$rc"
