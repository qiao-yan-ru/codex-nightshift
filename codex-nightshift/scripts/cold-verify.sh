#!/usr/bin/env bash
# 每个根脚本独立冷启动验证。
#
# 为什么必须独立：实测中连着跑时，前一条命令构建的产物会被后一条复用，
# **掩盖后一条缺失的构建步骤**——本地两次全绿，CI 红。
#
# 用法: cold-verify.sh <worktree路径> [脚本名...]
#       cold-verify.sh --self-test        # 只验失败分类逻辑，不需要仓库
# 默认依次验证: test:unit test:integration build typecheck
#
# 环境变量:
#   DATABASE_URL  设置后 test:integration 会真跑（不设则仅该脚本允许以
#                 "DATABASE_URL is required" 或连接失败结束）
#   CI=true       推荐设置，避免 pnpm 在非 TTY 下的交互提示

set -uo pipefail

WORKTREE="${1:-}"
shift || true

# --self-test 在参数校验之前处理：它不需要仓库
if [ "$WORKTREE" = "--self-test" ]; then
  SELF_TEST_ONLY=1
fi

if [ -z "${SELF_TEST_ONLY:-}" ] && { [ -z "$WORKTREE" ] || [ ! -d "$WORKTREE" ]; }; then
  echo "用法: $(basename "$0") <worktree路径> [脚本名...]" >&2
  echo "      $(basename "$0") --self-test" >&2
  exit 2
fi

SCRIPTS=("$@")
if [ ${#SCRIPTS[@]} -eq 0 ]; then
  SCRIPTS=(test:unit test:integration build typecheck)
fi

export CI="${CI:-true}"

clean() {
  find "$WORKTREE/packages" "$WORKTREE/apps" -type d -name dist \
    -not -path '*/node_modules/*' -exec rm -rf {} + 2>/dev/null
  find "$WORKTREE/packages" "$WORKTREE/apps" -name '*.tsbuildinfo' \
    -not -path '*/node_modules/*' -delete 2>/dev/null
}

summary() {
  # 只抽取结论行，过滤掉应用日志噪声
  grep -E "^ Test Files|^      Tests|Done$|Compiled successfully|Failed to resolve|is required|FAIL " \
    | head -8
}

# 失败原因分类。抽成独立函数以便 --self-test 验证（分类错了会把
# "环境缺失"误报成"代码问题"，或反过来放过真实缺陷）。
classify_failure() {
  local out="$1"
  local script="$2"
  # 依赖没装：几乎所有脚本都会以奇怪的方式失败，先排除
  if printf '%s' "$out" | grep -q "node_modules missing"; then
    echo "deps"
  elif printf '%s' "$out" | grep -q "Failed to resolve entry for package"; then
    echo "module-resolution"
  elif [ "$script" = "test:integration" ] && \
       printf '%s' "$out" | grep -qE "DATABASE_URL.*is required|ECONNREFUSED"; then
    echo "external-dependency"
  else
    echo "real"
  fi
}

self_test() {
  local failures=0
  check() {
    local name="$1" script="$2" input="$3" want="$4"
    local got
    got=$(classify_failure "$input" "$script")
    if [ "$got" = "$want" ]; then
      echo "  ✅ ${name}"
    else
      echo "  ❌ ${name}: 期望 ${want}，实际 ${got}"
      failures=$((failures + 1))
    fi
  }
  echo "== classify_failure 自检 =="
  check "依赖未安装"        "build"            "WARN  Local package.json exists, but node_modules missing"  "deps"
  check "模块解析失败"      "test:unit"        "Error: Failed to resolve entry for package \"@x/y\"."       "module-resolution"
  check "集成测试缺数据库"  "test:integration" "Error: DATABASE_URL is required"                            "external-dependency"
  check "集成测试连接被拒"  "test:integration" "Error: connect ECONNREFUSED 127.0.0.1:1"                    "external-dependency"
  check "构建缺配置"        "build"            "Error: API_KEY is required"                                "real"
  check "单测连接被拒"      "test:unit"        "Error: connect ECONNREFUSED 127.0.0.1:1"                    "real"
  check "真实断言失败"      "test:unit"        "AssertionError: expected 1 to be 2"                         "real"
  check "空输出"            "typecheck"        ""                                                            "real"
  echo
  [ "$failures" -eq 0 ] && echo "自检通过" || echo "自检失败：${failures} 项"
  return "$failures"
}

if [ "$WORKTREE" = "--self-test" ]; then
  self_test
  exit $?
fi

OVERALL=0
for script in "${SCRIPTS[@]}"; do
  echo "════════════════════════════════════════"
  echo "  冷启动: pnpm $script"
  echo "════════════════════════════════════════"
  clean

  REMAIN=$(find "$WORKTREE/packages" "$WORKTREE/apps" -type d -name dist \
    -not -path '*/node_modules/*' 2>/dev/null | wc -l | tr -d ' ')
  echo "  清理后剩余 dist 目录: $REMAIN"

  OUT=$(cd "$WORKTREE" && pnpm "$script" 2>&1)
  RC=$?
  echo "$OUT" | summary

  if [ "$RC" -eq 0 ]; then
    echo "  → 通过"
  else
    echo "  → 失败（退出码 ${RC}）"
    # 关键区分：失败原因是"环境缺失"还是"代码问题"
    case "$(classify_failure "$OUT" "$script")" in
      deps)
        echo "  ⚠️  依赖未安装——先在 $WORKTREE 跑 pnpm install，否则结果无意义"
        OVERALL=1 ;;
      module-resolution)
        echo "  ⚠️  模块解析失败——这是构建步骤缺失，不是环境问题"
        OVERALL=1 ;;
      external-dependency)
        echo "  ℹ️  因缺少外部依赖（数据库等）失败，符合预期；需在有该依赖的环境重跑" ;;
      *)
        echo "  ⚠️  真实失败，需排查"
        OVERALL=1 ;;
    esac
  fi
  echo
done

if [ "$OVERALL" -eq 0 ]; then
  echo "全部脚本通过（或因外部依赖缺失而符合预期地失败）。"
else
  echo "存在需要处理的问题。"
fi
exit "$OVERALL"
