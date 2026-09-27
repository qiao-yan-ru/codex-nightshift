#!/usr/bin/env bash
# 把审查反馈打回给同一会话——保留它对自身实现的记忆。
#
# 用法: codex-resume.sh <session-id> <反馈文件> [输出目录]
#
# 注意：resume 子命令不接受 --sandbox，必须用 -c sandbox_mode= 覆盖。

set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=codex-common.sh
. "$SCRIPT_DIR/codex-common.sh"

SESSION="${1:-}"
FEEDBACK="${2:-}"
OUTDIR="${3:-}"

if [ -z "$SESSION" ] || [ -z "$FEEDBACK" ]; then
  echo "用法: $(basename "$0") <session-id> <反馈文件> [输出目录]" >&2
  exit 2
fi

if [ ! -f "$FEEDBACK" ]; then
  echo "反馈文件不存在：$FEEDBACK" >&2
  exit 5
fi

load_loop_role_config implementation "${CODEX_LOOP_PROJECT:-}" || exit $?
resolve_codex || exit $?

if [ -z "$OUTDIR" ]; then
  OUTDIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-resume.XXXXXX")"
fi
if [ -d "$OUTDIR" ] && find "$OUTDIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "输出目录必须为空，避免混用旧报告：$OUTDIR" >&2
  exit 8
fi
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"
write_runtime_config "$OUTDIR" implementation

echo "session  : $SESSION"
echo "反馈文件 : $FEEDBACK"
echo "输出目录 : $OUTDIR"
echo "模型/推理: $LOOP_MODEL / $LOOP_REASONING_EFFORT"
echo

run_codex_with_watchdog \
  "$OUTDIR/events.jsonl" \
  "$OUTDIR/stderr.log" \
  "$FEEDBACK" \
  "$OUTDIR/watchdog.json" \
  "${CODEX_ROLE_ARGS[@]}" \
  exec resume \
  --skip-git-repo-check \
  --disable hooks \
  -c sandbox_mode="workspace-write" \
  -c sandbox_workspace_write.network_access=true \
  --json \
  --output-last-message "$OUTDIR/report.md" \
  "$SESSION" -
RC=$?

if [ "$RC" -eq 0 ] && [ ! -s "$OUTDIR/report.md" ]; then
  echo "Codex 返回成功，但没有生成最终报告。" >&2
  RC=7
fi

echo "── 退出码: $RC ──"
echo "watchdog : $(cat "$OUTDIR/watchdog.json" 2>/dev/null || echo '（无状态）')"
echo
cat "$OUTDIR/report.md" 2>/dev/null || echo "（无报告）"
echo
echo "返工后仍需按 verification-gates.md 独立验证，重点重做回退验证。"

exit "$RC"
