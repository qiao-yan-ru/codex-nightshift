#!/usr/bin/env bash
# 用全新的只读 Codex session 审查当前任务的未提交改动。
#
# 用法: codex-review.sh <worktree路径> <review任务包文件> [输出目录]
# 输出: events.jsonl  review.json  stderr.log  session-id.txt  watchdog.json
#       target-before.json  target-after.json

set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=codex-common.sh
. "$SCRIPT_DIR/codex-common.sh"

WORKTREE="${1:-}"
PACKET="${2:-}"
OUTDIR="${3:-}"
SCHEMA="$SCRIPT_DIR/review-result.schema.json"

if [ -z "$WORKTREE" ] || [ -z "$PACKET" ]; then
  echo "用法: $(basename "$0") <worktree路径> <review任务包文件> [输出目录]" >&2
  exit 2
fi

if [ ! -d "$WORKTREE" ]; then
  echo "worktree 不存在：$WORKTREE" >&2
  exit 4
fi
if ! git -C "$WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "review 必须在 Git worktree 中运行：$WORKTREE" >&2
  exit 4
fi
if [ ! -f "$PACKET" ]; then
  echo "review 任务包不存在：$PACKET" >&2
  exit 5
fi
if [ ! -f "$SCHEMA" ]; then
  echo "review 输出 schema 不存在：$SCHEMA" >&2
  exit 5
fi

load_loop_role_config review "$WORKTREE" || exit $?
resolve_codex || exit $?

WORKTREE="$(python3 - "$WORKTREE" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
if [ -z "$OUTDIR" ]; then
  OUTDIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-review.XXXXXX")"
fi
OUTDIR="$(python3 - "$OUTDIR" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
case "$OUTDIR/" in
  "$WORKTREE/"*)
    echo "review 输出目录必须位于 worktree 之外，避免改变被审查对象：$OUTDIR" >&2
    exit 8
    ;;
esac
if [ -d "$OUTDIR" ] && find "$OUTDIR" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  echo "输出目录必须为空，避免混用旧 review：$OUTDIR" >&2
  exit 8
fi
mkdir -p "$OUTDIR"
write_runtime_config "$OUTDIR" review

if [ -z "$(git -C "$WORKTREE" status --porcelain --untracked-files=all)" ]; then
  echo "worktree 没有未提交改动，无法执行 --uncommitted review。" >&2
  exit 9
fi

python3 "$SCRIPT_DIR/worktree-fingerprint.py" "$WORKTREE" "$OUTDIR/target-before.json"

echo "worktree   : $WORKTREE"
echo "review 包  : $PACKET"
echo "输出目录   : $OUTDIR"
echo "模型/推理  : $LOOP_MODEL / $LOOP_REASONING_EFFORT"
echo "目标 HEAD  : $(git -C "$WORKTREE" rev-parse --short HEAD)"
echo

# 全局 -s read-only 和 review 子命令共同保证 reviewer 只审不改。
# reviewer 不接收 implementer 的报告，只接收 review 包和实际 diff。
run_codex_with_watchdog \
  "$OUTDIR/events.jsonl" \
  "$OUTDIR/stderr.log" \
  "$PACKET" \
  "$OUTDIR/watchdog.json" \
  "${CODEX_ROLE_ARGS[@]}" \
  -C "$WORKTREE" \
  -s read-only \
  exec review \
  --uncommitted \
  --disable hooks \
  --json \
  --output-schema "$SCHEMA" \
  --output-last-message "$OUTDIR/review.json" \
  -
RC=$?

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
  echo "Codex review 返回成功，但事件流中没有 thread.started/session id。" >&2
  RC=6
fi
if [ "$RC" -eq 0 ] && [ ! -s "$OUTDIR/review.json" ]; then
  echo "Codex review 返回成功，但没有结构化结果。" >&2
  RC=7
fi
if [ "$RC" -eq 0 ]; then
  python3 - "$OUTDIR/review.json" <<'PY' || RC=7
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
verdict = result.get("verdict")
findings = result.get("findings")
if verdict not in {"pass", "changes_requested", "blocked"} or not isinstance(findings, list):
    raise SystemExit("review.json 缺少合法 verdict/findings")
if verdict == "pass" and findings:
    raise SystemExit("verdict=pass 时 findings 必须为空")
if verdict == "changes_requested" and not findings:
    raise SystemExit("verdict=changes_requested 时 findings 不能为空")
PY
fi

python3 "$SCRIPT_DIR/worktree-fingerprint.py" "$WORKTREE" "$OUTDIR/target-after.json"
if ! python3 - "$OUTDIR/target-before.json" "$OUTDIR/target-after.json" <<'PY'
import json, sys
before = json.load(open(sys.argv[1], encoding="utf-8"))
after = json.load(open(sys.argv[2], encoding="utf-8"))
raise SystemExit(0 if before["fingerprint"] == after["fingerprint"] else 1)
PY
then
  echo "review 期间 worktree 发生变化，结论已过期，必须重新 review。" >&2
  RC=10
fi

echo "── review 退出码: $RC ──"
echo "session id: ${SESSION_ID:-（未取到）}"
echo "watchdog : $(cat "$OUTDIR/watchdog.json" 2>/dev/null || echo '（无状态）')"
if [ -s "$OUTDIR/review.json" ]; then
  python3 - "$OUTDIR/review.json" <<'PY' 2>/dev/null || true
import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"verdict  : {r.get('verdict', 'unknown')}")
print(f"findings : {len(r.get('findings', []))}")
PY
fi

exit "$RC"
