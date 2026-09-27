#!/usr/bin/env bash
# Codex 实现、返工、独立 review 与 UI QA 共用的二进制发现和 watchdog。

resolve_codex() {
  local legacy_codex="$HOME/.codex/plugins/.plugin-appserver/codex"

  if [ -n "${CODEX_BIN:-}" ]; then
    CODEX="$(command -v "$CODEX_BIN" 2>/dev/null || true)"
    if [ -z "$CODEX" ]; then
      echo "CODEX_BIN 指向的命令不可执行：$CODEX_BIN" >&2
      return 3
    fi
  elif CODEX="$(command -v codex 2>/dev/null)" && [ -n "$CODEX" ]; then
    :
  elif [ -x "$legacy_codex" ]; then
    CODEX="$legacy_codex"
  else
    echo "找不到 codex 二进制。" >&2
    echo "请安装到 PATH，或设置 CODEX_BIN=/path/to/codex。" >&2
    return 3
  fi
}

# 解析某个角色的合并配置。首次配置未完成时返回 20，且不会启动 Codex。
load_loop_role_config() {
  local role="$1"
  local project="${2:-}"
  local runtime_output rc
  local command=(python3 "$SCRIPT_DIR/loop_config.py" runtime --role "$role")

  if [ -n "$project" ]; then
    command+=(--project "$project")
  fi
  runtime_output="$("${command[@]}")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ -n "$runtime_output" ]; then
      printf '%s\n' "$runtime_output" >&2
    fi
    return "$rc"
  fi

  IFS=$'\036' read -r \
    LOOP_MODEL \
    LOOP_REASONING_EFFORT \
    LOOP_QA_DRIVER \
    LOOP_QA_ENVIRONMENT \
    LOOP_QA_MODE \
    LOOP_POLL_SECONDS \
    LOOP_IDLE_TIMEOUT_SECONDS \
    LOOP_MAX_RUNTIME_SECONDS \
    LOOP_TERMINATE_GRACE_SECONDS \
    LOOP_CONFIG_FINGERPRINT <<EOF
$runtime_output
EOF

  if [ -z "${LOOP_MODEL:-}" ] || [ -z "${LOOP_REASONING_EFFORT:-}" ] || \
     [ -z "${LOOP_CONFIG_FINGERPRINT:-}" ]; then
    echo "配置运行时输出不完整，拒绝启动 Codex。" >&2
    return 2
  fi

  # loop_config.py 已经处理一次性环境覆盖；若使用冻结快照，它会忽略后续环境漂移。
  CODEX_POLL_SECONDS="$LOOP_POLL_SECONDS"
  CODEX_IDLE_TIMEOUT_SECONDS="$LOOP_IDLE_TIMEOUT_SECONDS"
  CODEX_MAX_RUNTIME_SECONDS="$LOOP_MAX_RUNTIME_SECONDS"
  CODEX_TERMINATE_GRACE_SECONDS="$LOOP_TERMINATE_GRACE_SECONDS"

  CODEX_ROLE_ARGS=()
  if [ "$LOOP_MODEL" != "inherit" ]; then
    CODEX_ROLE_ARGS+=(-m "$LOOP_MODEL")
  fi
  if [ "$LOOP_REASONING_EFFORT" != "inherit" ]; then
    CODEX_ROLE_ARGS+=(-c "model_reasoning_effort=\"$LOOP_REASONING_EFFORT\"")
  fi
}

write_runtime_config() {
  local output_dir="$1"
  local role="$2"
  python3 - \
    "$output_dir/runtime-config.json" \
    "$role" \
    "$LOOP_MODEL" \
    "$LOOP_REASONING_EFFORT" \
    "$LOOP_QA_MODE" \
    "$LOOP_CONFIG_FINGERPRINT" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "role": sys.argv[2],
    "model": sys.argv[3],
    "reasoning_effort": sys.argv[4],
    "qa_mode": sys.argv[5],
    "config_fingerprint": sys.argv[6],
}
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

require_nonnegative_integer() {
  local name="$1"
  local value="$2"
  case "$value" in
    ''|*[!0-9]*)
      echo "$name 必须是非负整数，实际为：$value" >&2
      return 2 ;;
  esac
}

write_watchdog_status() {
  local status_file="$1"
  local result="$2"
  local exit_code="$3"
  local pid="$4"
  local runtime="$5"
  printf '{"result":"%s","exit_code":%s,"pid":%s,"runtime_seconds":%s}\n' \
    "$result" "$exit_code" "$pid" "$runtime" > "$status_file"
}

terminate_codex_process() {
  local pid="$1"
  local grace_seconds="$2"
  local waited=0

  kill -TERM "$pid" 2>/dev/null || true
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$grace_seconds" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

# 用法: run_codex_with_watchdog <events> <stderr> <stdin> <status> <codex参数...>
# 返回 Codex 的退出码；idle/max-runtime 超时统一返回 124。
run_codex_with_watchdog() {
  local events_file="$1"
  local stderr_file="$2"
  local input_file="$3"
  local status_file="$4"
  shift 4

  local poll_seconds="${CODEX_POLL_SECONDS:-20}"
  local idle_timeout="${CODEX_IDLE_TIMEOUT_SECONDS:-1200}"
  local max_runtime="${CODEX_MAX_RUNTIME_SECONDS:-7200}"
  local grace_seconds="${CODEX_TERMINATE_GRACE_SECONDS:-10}"

  require_nonnegative_integer CODEX_POLL_SECONDS "$poll_seconds" || return $?
  require_nonnegative_integer CODEX_IDLE_TIMEOUT_SECONDS "$idle_timeout" || return $?
  require_nonnegative_integer CODEX_MAX_RUNTIME_SECONDS "$max_runtime" || return $?
  require_nonnegative_integer CODEX_TERMINATE_GRACE_SECONDS "$grace_seconds" || return $?
  if [ "$poll_seconds" -eq 0 ]; then
    echo "CODEX_POLL_SECONDS 必须大于 0" >&2
    return 2
  fi

  "$CODEX" "$@" < "$input_file" > "$events_file" 2> "$stderr_file" &
  local codex_pid=$!
  local started_at now last_progress_at runtime bytes last_bytes=0
  local reason=""
  started_at=$(date +%s)
  last_progress_at="$started_at"
  write_watchdog_status "$status_file" "running" 0 "$codex_pid" 0

  while kill -0 "$codex_pid" 2>/dev/null; do
    sleep "$poll_seconds"
    now=$(date +%s)
    runtime=$((now - started_at))
    bytes=$(wc -c < "$events_file" 2>/dev/null | tr -d ' ')
    bytes="${bytes:-0}"
    if [ "$bytes" -ne "$last_bytes" ]; then
      last_bytes="$bytes"
      last_progress_at="$now"
    fi

    if [ "$max_runtime" -gt 0 ] && [ "$runtime" -ge "$max_runtime" ]; then
      reason="max-runtime"
      break
    fi
    if [ "$idle_timeout" -gt 0 ] && [ $((now - last_progress_at)) -ge "$idle_timeout" ]; then
      reason="idle-timeout"
      break
    fi
  done

  if [ -n "$reason" ]; then
    terminate_codex_process "$codex_pid" "$grace_seconds"
    wait "$codex_pid" 2>/dev/null || true
    now=$(date +%s)
    runtime=$((now - started_at))
    write_watchdog_status "$status_file" "$reason" 124 "$codex_pid" "$runtime"
    printf 'Codex 被 watchdog 终止：%s (%ss)\n' "$reason" "$runtime" >&2
    return 124
  fi

  wait "$codex_pid"
  local rc=$?
  now=$(date +%s)
  runtime=$((now - started_at))
  if [ "$rc" -eq 0 ]; then
    write_watchdog_status "$status_file" "completed" "$rc" "$codex_pid" "$runtime"
  else
    write_watchdog_status "$status_file" "failed" "$rc" "$codex_pid" "$runtime"
  fi
  return "$rc"
}
