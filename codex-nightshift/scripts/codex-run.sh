#!/usr/bin/env bash
# 在隔离 worktree 里启动一次 codex 实现。
#
# 用法: codex-run.sh <worktree路径> <任务包文件> [输出目录]
# 输出: <输出目录>/events.jsonl  <输出目录>/report.md  <输出目录>/stderr.log
#       <输出目录>/session-id.txt  <输出目录>/watchdog.json
#
# 脚本自带 watchdog；可由编排者前台等待，也可交给持久化后台进程。

set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=codex-common.sh
. "$SCRIPT_DIR/codex-common.sh"

WORKTREE="${1:-}"
PACKET="${2:-}"
OUTDIR="${3:-}"

if [ -z "$WORKTREE" ] || [ -z "$PACKET" ]; then
  echo "用法: $(basename "$0") <worktree路径> <任务包文件> [输出目录]" >&2
  exit 2
fi

if [ ! -d "$WORKTREE" ]; then
  echo "worktree 不存在：$WORKTREE" >&2
  exit 4
fi

if [ ! -f "$PACKET" ]; then
  echo "任务包不存在：$PACKET" >&2
  exit 5
fi

load_loop_role_config implementation "$WORKTREE" || exit $?
resolve_codex || exit $?

if [ -z "$OUTDIR" ]; then
  OUTDIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-run.XXXXXX")"
fi
if [ -d "$OUTDIR" ] && find "$OUTDIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "输出目录必须为空，避免混用旧报告：$OUTDIR" >&2
  exit 8
fi
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd)"
write_runtime_config "$OUTDIR" implementation

echo "worktree : $WORKTREE"
echo "任务包   : $PACKET"
echo "输出目录 : $OUTDIR"
echo "模型/推理: $LOOP_MODEL / $LOOP_REASONING_EFFORT"
echo "分支     : $(git -C "$WORKTREE" branch --show-current 2>/dev/null || echo '未知')"
echo

# 关键：必须显式重定向 stdin（- < 文件），否则会永久阻塞在
# "Reading additional input from stdin..."。
# --disable hooks：避免宿主注入的 hook 在本次运行中触发。
# network_access=true：安装依赖所必需。
run_codex_with_watchdog \
  "$OUTDIR/events.jsonl" \
  "$OUTDIR/stderr.log" \
  "$PACKET" \
  "$OUTDIR/watchdog.json" \
  "${CODEX_ROLE_ARGS[@]}" \
  exec \
  --sandbox workspace-write \
  --skip-git-repo-check \
  --disable hooks \
  -c sandbox_workspace_write.network_access=true \
  -C "$WORKTREE" \
  --json \
  --output-last-message "$OUTDIR/report.md" \
  -
RC=$?

# 取 session id，供后续 resume 打回使用
python3 - "$OUTDIR/events.jsonl" > "$OUTDIR/session-id.txt" 2>/dev/null <<'PY' || true
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    if event.get("type") == "thread.started":
        print(event["thread_id"])
        break
PY

SESSION_ID="$(sed -n '1p' "$OUTDIR/session-id.txt" 2>/dev/null || true)"
if [ "$RC" -eq 0 ] && [ -z "$SESSION_ID" ]; then
  echo "Codex 返回成功，但事件流中没有 thread.started/session id。" >&2
  RC=6
fi
if [ "$RC" -eq 0 ] && [ ! -s "$OUTDIR/report.md" ]; then
  echo "Codex 返回成功，但没有生成最终报告。" >&2
  RC=7
fi

echo "── 退出码: $RC ──"
python3 - "$OUTDIR/events.jsonl" <<'PY' 2>/dev/null || true
import collections, json, sys
counts = collections.Counter()
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    try:
        counts[json.loads(line).get("type", "?")] += 1
    except json.JSONDecodeError:
        counts["<parse_error>"] += 1
for key, value in counts.most_common():
    print(f"  {key}: {value}")
PY

echo
echo "session id: ${SESSION_ID:-（未取到）}"
echo "watchdog : $(cat "$OUTDIR/watchdog.json" 2>/dev/null || echo '（无状态）')"
echo
echo "下一步：进入 reviewing，启动全新只读 reviewer；通过仲裁后再执行确定性门禁。"
echo "注意：实现报告和 reviewer verdict 都只是证据，不等于最终验收。"

exit "$RC"
