#!/usr/bin/env python3
"""Opt-in, session-bound Claude Code continuation hooks (POSIX, stdlib only)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid


SCRIPT = Path(__file__).resolve()
STATE_SCRIPT = SCRIPT.with_name("run-state.py")
SESSION_ENV = "CODEX_LOOP_CC_SESSION_ID"
BINDINGS_ENV = "CODEX_LOOP_BINDINGS_DIR"
ACTIONABLE = {"ready", "recover", "park", "replan", "complete"}
PAUSED = {"paused", "stopped", "cancelled", "canceled", "aborted"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def binding_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def binding_path(directory: Path, session_id: str) -> Path:
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return directory / f"{name}.json"


def session_id(args: argparse.Namespace) -> str:
    value = args.session_id or os.environ.get(SESSION_ENV, "")
    if not value or any(char in value for char in "\r\n\x00"):
        raise ValueError(
            f"Missing current Claude Code session ID. Install hooks, then start/resume "
            f"Claude Code so SessionStart exports {SESSION_ENV}; or pass a known "
            "current --session-id explicitly. Never infer the latest session."
        )
    return value


def next_action(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_json(path)
    if state.get("schema_version") != 1 or not isinstance(state.get("tasks"), list):
        raise ValueError(f"Unsupported run state: {path}")
    if state.get("status") in PAUSED:
        return state, {"result": "paused"}
    result = subprocess.run(
        [sys.executable, str(STATE_SCRIPT), "next", str(path)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise ValueError(f"run-state.py next failed: {result.stderr.strip()[:1000]}")
    action = json.loads(result.stdout)
    if not isinstance(action, dict) or action.get("result") not in ACTIONABLE | {"deadline", "blocked"}:
        raise ValueError("run-state.py next returned an unsupported result")
    return state, action


def worker_activity(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Read only current-worker file metadata and tiny watchdog JSON, never logs."""
    evidence = []
    waiting = []
    budgets = (state.get("config_snapshot") or {}).get("watchdog", {})
    idle_limit = budgets.get("idle_timeout_seconds", 1200)
    runtime_limit = budgets.get("max_runtime_seconds", 7200)
    groups = {"running": "runs", "reviewing": "reviews", "qa_running": "qa_runs"}
    for task in state["tasks"]:
        runs = task.get(groups.get(task["status"], ""), [])
        if not runs or not runs[-1].get("output_dir"):
            continue
        directory = Path(runs[-1]["output_dir"])
        files = {}
        for name in ("events.jsonl", "stderr.log", "watchdog.json", "report.md", "review.json", "qa-result.json"):
            try:
                info = (directory / name).stat()
                files[name] = [info.st_size, info.st_mtime_ns]
            except FileNotFoundError:
                pass
        evidence.append({"task": task["id"], "directory": str(directory), "files": files})
        try:
            watchdog_path = directory / "watchdog.json"
            if watchdog_path.stat().st_size > 65536:
                continue
            watchdog = load_json(watchdog_path)
            pid = watchdog.get("pid")
            if watchdog.get("result") != "running" or not isinstance(pid, int) or pid <= 0:
                continue
            os.kill(pid, 0)  # Existence probe only; never terminates a process.
            started = watchdog_path.stat().st_mtime
            if runs[-1].get("started_at"):
                started = datetime.fromisoformat(runs[-1]["started_at"].replace("Z", "+00:00")).timestamp()
            last_output = files.get("events.jsonl", [0, int(started * 1e9)])[1] / 1e9
            current = time.time()
            # With both watchdog limits disabled, retain the bounded Stop fallback.
            if (idle_limit or runtime_limit) and (not runtime_limit or current - started < runtime_limit) and (not idle_limit or current - last_output < idle_limit):
                waiting.append(task["id"])
        except (OSError, ValueError, TypeError):
            pass  # A partial watchdog write is not proof of a live bounded worker.
    return evidence, waiting


def progress_fingerprint(state: dict[str, Any], action: dict[str, Any], activity: list[dict[str, Any]]) -> str:
    # Notes, state timestamps and history cannot pretend to be task progress.
    keys = (
        "id", "status", "depends_on", "attempts", "review_attempts", "qa_attempts",
        "commit", "checkpoint_artifact", "blocked_artifact", "parked", "stash_ref",
        "qa_audit_decision", "replaces", "replaced_by", "replan_pending",
    )
    tasks = []
    for task in state["tasks"]:
        item = {key: task.get(key) for key in keys}
        for key in ("sessions", "review_sessions", "qa_sessions"):
            item[key] = [entry.get("id") for entry in task.get(key, [])]
        for key in ("runs", "reviews", "qa_runs"):
            item[key] = [
                {field: run.get(field) for field in ("status", "session_id", "decision", "verdict")}
                for run in task.get(key, [])
            ]
        tasks.append(item)
    value = {"result": action["result"], "tasks": tasks, "replan_count": state.get("replan_count", 0), "worker_activity": activity}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def deactivate(binding: dict[str, Any], reason: str) -> None:
    binding.update(armed=False, disarm_reason=reason, updated_at=now())


def command_install(args: argparse.Namespace) -> None:
    settings_path = Path(args.settings).expanduser().resolve()
    settings = load_json(settings_path) if settings_path.exists() else {}
    if settings.get("disableAllHooks"):
        raise ValueError("disableAllHooks is enabled; hooks were not installed or enabled")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.hooks must be an object; refusing to replace it")
    command = shlex.join([sys.executable, str(SCRIPT), "hook", "--bindings-dir", str(args.bindings_dir)])
    changed = False
    for event in ("SessionStart", "Stop"):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings.hooks.{event} must be an array")
        found = any(
            isinstance(group, dict) and not group.get("matcher")
            and any(isinstance(hook, dict) and hook.get("type") == "command"
                    and hook.get("command") == command for hook in group.get("hooks", []))
            for group in groups
        )
        if not found:
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 20}]})
            changed = True
    backup = None
    if changed:
        if settings_path.exists():
            backup = settings_path.with_name(f"{settings_path.name}.codex-loop-backup-{uuid.uuid4().hex}.json")
            # Preserve the original bytes (including formatting), not a reserialization.
            backup.write_bytes(settings_path.read_bytes())
            os.chmod(backup, settings_path.stat().st_mode & 0o777)
        write_json(settings_path, settings)
    print(json.dumps({
        "installed": True, "changed": changed, "settings": str(settings_path),
        "bindings_dir": str(args.bindings_dir), "backup": str(backup) if backup else None,
        "next": "Start/resume Claude Code to load SessionStart; then explicitly arm the run. Installation alone never arms a session.",
    }, ensure_ascii=False))


def command_arm(args: argparse.Namespace) -> None:
    sid = session_id(args)
    path = Path(args.state).expanduser().resolve()
    state, action = next_action(path)
    if action["result"] not in ACTIONABLE:
        raise ValueError(f"Cannot arm run whose next action is {action['result']}")
    worktree = Path(state["worktree"]).expanduser().resolve()
    if not worktree.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree}")
    config = Path(args.config).expanduser().resolve() if args.config else path.parent / "resolved-config.json"
    if args.config and not config.is_file():
        raise ValueError(f"Config snapshot does not exist: {config}")
    with binding_lock(args.bindings_dir):
        target = binding_path(args.bindings_dir, sid)
        for existing in args.bindings_dir.glob("*.json"):
            record = load_json(existing)
            if not record.get("armed"):
                continue
            if record.get("state") == str(path) and record.get("session_id") != sid:
                raise ValueError("Run is already armed by another Claude Code session; disarm that session explicitly before handing off")
            if existing == target:
                raise ValueError("This session is already armed; disarm explicitly before re-arming or changing its run")
        binding = {
            "schema_version": 1, "session_id": sid, "state": str(path),
            "worktree": str(worktree), "config": str(config) if config.is_file() else None,
            "armed": True, "max_stalled_stops": args.max_stalled_stops,
            "last_progress": None, "stalled_stops": 0, "created_at": now(), "updated_at": now(),
        }
        write_json(target, binding)
    print(json.dumps(binding, ensure_ascii=False))


def command_disarm(args: argparse.Namespace) -> None:
    sid = session_id(args)
    with binding_lock(args.bindings_dir):
        target = binding_path(args.bindings_dir, sid)
        if not target.exists():
            print(json.dumps({"session_id": sid, "armed": False, "reason": "not-bound"}))
            return
        binding = load_json(target)
        deactivate(binding, args.reason)
        write_json(target, binding)
    print(json.dumps(binding, ensure_ascii=False))


def command_status(args: argparse.Namespace) -> None:
    if args.all:
        values = [load_json(path) for path in sorted(args.bindings_dir.glob("*.json"))]
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return
    sid = session_id(args)
    target = binding_path(args.bindings_dir, sid)
    value = load_json(target) if target.exists() else {"session_id": sid, "armed": False, "reason": "not-bound"}
    print(json.dumps(value, ensure_ascii=False, indent=2))


def recovery_context(binding: dict[str, Any], state: dict[str, Any], action: dict[str, Any]) -> str:
    tasks = action.get("tasks", [action["task"]] if "task" in action else [])
    compact_tasks = [{key: task.get(key) for key in ("id", "status", "packet", "replan_pending")} for task in tasks[:3]]
    config = binding.get("config") or f"{binding['state']}#config_snapshot"
    contract = Path(binding["state"]).parent / "run-contract.md"
    contract_line = f"run_contract={contract}\n" if contract.is_file() else ""
    return (
        "codex-nightshift 恢复上下文（只继续此已授权 run）：\n"
        f"state={binding['state']}\nconfig={config}\nworktree={binding['worktree']}\n"
        f"{contract_line}"
        f"checkpoint_mode={state.get('checkpoint_mode', 'commit')}; deadline={state.get('deadline') or 'none'}\n"
        f"next={action['result']}; tasks={json.dumps(compact_tasks, ensure_ascii=False)}\n"
        "先读取 run-contract.md 中的目标/授权/预算、状态摘要和当前任务包；检查当前 worker 是否仍运行，勿重复启动。每个新小任务启动全新 Codex session。"
        "只保留当前任务需要的上下文，详细日志留在磁盘。用户要求暂停/停止时先 disarm，不得自行重新 arm。"
    )


def continue_reason(binding: dict[str, Any], state: dict[str, Any], action: dict[str, Any], directory: Path) -> str:
    operations = {
        "ready": "队列还有可执行任务；立即读取下一个任务包并启动新的 Codex session，完成后继续选取任务。",
        "recover": "仍有执行中或待验收任务；检查该任务的 worker/日志和落盘结果，恢复对应阶段，不要重复启动仍存活的 worker。worker 尚在 watchdog 时限内时在本轮持续等待/检查，勿以最终答复结束等待。",
        "park": "有任务需要停放或结束重拆意图；" + action.get("action", "保存现场、记录 blocked/停放证据，再调用 next 继续其他独立任务。"),
        "replan": "有已停放的过大任务待重拆；读取原目标、授权边界和重拆依据，在 max_replans 预算内创建更小且覆盖原验收条件的任务包，执行 split 后继续 next。不能扩大目标或无限重拆。",
        "complete": "任务队列已完成；执行并记录整体集成验收、整理最终摘要，然后 disarm --reason completed 并结束。不得仅因 next=complete 省略整体验收。",
    }
    disarm = shlex.join([sys.executable, str(SCRIPT), "disarm", "--session-id", binding["session_id"], "--bindings-dir", str(directory), "--reason", "user-stop"])
    return (
        operations[action["result"]] + "\n" + recovery_context(binding, state, action)
        + "\n如果用户要求停止/暂停、需要新的授权或发生全局环境阻塞，先解除本会话接续再说明原因：\n"
        + disarm + "\n不要通过重复修改备注或时间戳规避无进展上限。"
    )


def command_hook(args: argparse.Namespace) -> None:
    event = json.load(sys.stdin)
    sid = event.get("session_id")
    kind = event.get("hook_event_name")
    if not isinstance(sid, str) or not sid or kind not in {"SessionStart", "Stop"}:
        return
    if event.get("agent_id"):
        return  # A main-session binding must never control a subagent.
    if kind == "SessionStart" and os.environ.get("CLAUDE_ENV_FILE"):
        with open(os.environ["CLAUDE_ENV_FILE"], "a", encoding="utf-8") as handle:
            handle.write(f"\nexport {SESSION_ENV}={shlex.quote(sid)}\n")
            handle.write(f"export {BINDINGS_ENV}={shlex.quote(str(args.bindings_dir))}\n")
    target = binding_path(args.bindings_dir, sid)
    if not target.exists():
        return
    with binding_lock(args.bindings_dir):
        binding = load_json(target)
        if not binding.get("armed") or binding.get("session_id") != sid:
            return
        state, action = next_action(Path(binding["state"]))
        if str(Path(state["worktree"]).expanduser().resolve()) != binding["worktree"]:
            raise ValueError("Bound worktree changed; inspect run state before explicitly re-arming")
        result = action["result"]
        if result not in ACTIONABLE:
            deactivate(binding, result)
            write_json(target, binding)
            message = f"Codex loop 已解除接续：{result}。保存当前现场并汇总已完成/阻塞任务；截止后不再启动新工作。state={binding['state']}"
            if kind == "SessionStart":
                print(json.dumps({"hookSpecificOutput": {"hookEventName": kind, "additionalContext": message}}, ensure_ascii=False))
            else:
                print(json.dumps({"systemMessage": message}, ensure_ascii=False))
            return
        if kind == "SessionStart":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": kind, "additionalContext": recovery_context(binding, state, action)}}, ensure_ascii=False))
            return
        activity, waiting = worker_activity(state)
        signature = progress_fingerprint(state, action, activity)
        count = binding.get("stalled_stops", 0) + 1 if binding.get("last_progress") == signature else 1
        if waiting:
            # Quiet live workers are governed by their watchdog, not by Stop frequency.
            count = 0
        binding.update(last_progress=signature, stalled_stops=count, updated_at=now(),
                       last_stop_hook_active=bool(event.get("stop_hook_active")))
        if count > binding["max_stalled_stops"]:
            deactivate(binding, "no-progress-stop-limit")
            write_json(target, binding)
            print(json.dumps({"systemMessage": f"Codex loop 已连续 {count - 1} 次接续而未观察到状态或 worker 输出变化，已解除接续；这不证明 worker 已退出，先检查进程/预算和 {binding['state']} 后才能显式重新 arm。"}, ensure_ascii=False))
            return
        write_json(target, binding)
        print(json.dumps({"decision": "block", "reason": continue_reason(binding, state, action, args.bindings_dir)}, ensure_ascii=False))


def positive_limit(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 7:
        raise argparse.ArgumentTypeError("must be between 1 and 7 (below Claude Code's native continuation cap)")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "arm", "disarm", "status", "hook"):
        sub = subs.add_parser(name)
        sub.add_argument("--bindings-dir", default=os.environ.get(BINDINGS_ENV, "~/.claude/codex-loop-bindings"))
        if name in {"arm", "disarm", "status"}:
            sub.add_argument("--session-id", default="")
        if name == "install":
            sub.add_argument("--settings", required=True, help="explicit Claude Code settings JSON path")
        elif name == "arm":
            sub.add_argument("state")
            sub.add_argument("--config", default="")
            sub.add_argument("--max-stalled-stops", type=positive_limit, default=3)
        elif name == "disarm":
            sub.add_argument("--reason", default="user-stop")
        elif name == "status":
            sub.add_argument("--all", action="store_true")
        sub.set_defaults(func=globals()[f"command_{name}"])
    args = parser.parse_args()
    args.bindings_dir = Path(args.bindings_dir).expanduser().resolve()
    try:
        args.func(args)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        # Failing hooks must not trap the user in an unbounded Stop loop.
        print(f"codex-loop: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
