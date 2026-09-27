#!/usr/bin/env python3
"""持久化无人值守 Codex 编排状态；只使用 Python 标准库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loop_config import (
    ConfigError,
    config_fingerprint,
    validate_config,
)
from loop_config import load_json as load_loop_config_json
from qa_result import ValidationError, validate_audit, validate_run

STATUSES = {
    "pending",
    "running",
    "reviewing",
    "verifying",
    "qa_running",
    "qa_auditing",
    "retrying",
    "done",
    "blocked",
    "superseded",
}
TRANSITIONS = {
    "pending": {"running", "blocked"},
    "running": {"reviewing", "retrying", "blocked"},
    "reviewing": {"verifying", "retrying", "blocked"},
    "verifying": {"done", "qa_running", "retrying", "blocked"},
    "qa_running": {"qa_auditing", "verifying", "blocked"},
    "qa_auditing": {"done", "retrying", "blocked"},
    "retrying": {"running", "blocked"},
    "blocked": {"pending", "blocked"},
    "done": set(),
    "superseded": set(),
}
ACTIVE_STATUSES = {"running", "reviewing", "verifying", "qa_running", "qa_auditing", "retrying"}
TERMINAL_STATUSES = {"done", "superseded"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_deadline(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        deadline = datetime.fromisoformat(normalized)
    except ValueError:
        fail(f"deadline 不是合法 ISO-8601 时间：{value}")
    if deadline.tzinfo is None:
        fail("deadline 必须包含时区，例如 2026-09-28T08:00:00+08:00")
    return deadline


def fail(message: str) -> None:
    raise SystemExit(message)


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"状态文件不存在：{path}")
    except json.JSONDecodeError as exc:
        fail(f"状态文件不是合法 JSON：{exc}")
    if state.get("schema_version") != 1 or not isinstance(state.get("tasks"), list):
        fail("状态文件 schema 不受支持")
    # 向后兼容独立 reviewer 引入前创建的状态文件。
    state.setdefault("max_review_attempts_per_task", 5)
    state.setdefault("max_qa_attempts_per_task", 5)
    state.setdefault("config_snapshot", {})
    state.setdefault("config_sha256", "")
    state.setdefault("checkpoint_mode", "commit")
    state.setdefault("max_replans", 2)
    state.setdefault("replan_count", 0)
    state.setdefault("replans", [])
    for task in state["tasks"]:
        task.setdefault("review_attempts", 0)
        task.setdefault("review_sessions", [])
        task.setdefault("reviews", [])
        task.setdefault("qa_required", False)
        task.setdefault("qa_attempts", 0)
        task.setdefault("qa_sessions", [])
        task.setdefault("qa_runs", [])
        task.setdefault("qa_audit_decision", "")
        task.setdefault("qa_audit_result", "")
        task.setdefault("task_kind", "unspecified")
        task.setdefault("parked", bool(task.get("blocked_artifact")) or task.get("attempts", 0) == 0)
        task.setdefault("checkpoint_artifact", "")
        task.setdefault("replaces", "")
        task.setdefault("replaced_by", [])
        task.setdefault("replan_pending", False)
        for run in task["runs"]:
            run.setdefault("model", "inherit")
            run.setdefault("reasoning_effort", "inherit")
        for review in task["reviews"]:
            review.setdefault("model", "inherit")
            review.setdefault("reasoning_effort", "inherit")
        for qa_run in task["qa_runs"]:
            qa_run.setdefault("reasoning_effort", "inherit")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def task_by_id(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in state["tasks"]:
        if task["id"] == task_id:
            return task
    fail(f"未知任务：{task_id}")


def add_history(state: dict[str, Any], task: dict[str, Any], event: str, note: str = "") -> None:
    state.setdefault("history", []).append(
        {"at": now(), "task_id": task["id"], "event": event, "note": note}
    )


def refresh_run_status(state: dict[str, Any]) -> None:
    tasks = state["tasks"]
    statuses = {task["id"]: task["status"] for task in tasks}
    if tasks and all(status in TERMINAL_STATUSES for status in statuses.values()):
        state["status"] = "completed"
    elif any(
        task["status"] in ACTIVE_STATUSES
        or (task["status"] == "blocked" and (not task.get("parked") or task.get("replan_pending")))
        or (task["status"] == "pending" and all(statuses.get(dep) == "done" for dep in task["depends_on"]))
        for task in tasks
    ):
        state["status"] = "active"
    else:
        state["status"] = "blocked"


def git(state: dict[str, Any], *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", state["worktree"], *arguments],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        fail(f"Git 检查失败：{result.stderr.strip()}")
    return result.stdout.strip()


def require_clean_worktree(state: dict[str, Any]) -> None:
    git(state, "rev-parse", "--verify", "HEAD")
    if git(state, "status", "--porcelain", "--untracked-files=all"):
        fail("worktree 不干净：必须先保存现场并安全停放改动；状态工具不会丢弃任何改动")


def require_artifact(state: dict[str, Any], value: str) -> tuple[str, str]:
    artifact = Path(value).expanduser().resolve()
    if not value or not artifact.is_file() or not artifact.stat().st_size:
        fail("必须提供实际存在且非空的 --artifact（补丁、归档或无改动说明）")
    if artifact.is_relative_to(Path(state["worktree"]).resolve()):
        fail("产物必须保存到 worktree 之外，避免后续清理影响恢复")
    return str(artifact), hashlib.sha256(artifact.read_bytes()).hexdigest()


def validate_task_id(task_id: Any) -> None:
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id):
        fail("任务 ID 必须是 1–128 位字母、数字、下划线、点或连字符，且以字母/数字开头")


def park_reason(state: dict[str, Any], task: dict[str, Any]) -> str:
    status = task["status"]
    if status == "blocked" and (
        not task.get("parked") or (task.get("replan_pending") and not task.get("blocked_artifact"))
    ):
        return "blocked_not_parked"
    if status == "blocked" and task.get("replan_pending") and state["replan_count"] >= state["max_replans"]:
        return "replan_budget_exhausted"
    if status in {"pending", "retrying"} and task["attempts"] >= state["max_retries_per_task"] + 1:
        return "implementation_budget_exhausted"
    if status == "reviewing" and task["review_attempts"] >= state["max_review_attempts_per_task"]:
        if not task["reviews"] or task["reviews"][-1]["status"] != "running":
            return "review_budget_exhausted"
    if status == "verifying" and task["qa_required"] and task["qa_attempts"] >= state["max_qa_attempts_per_task"]:
        return "qa_budget_exhausted"
    return ""


def validate_graph(state: dict[str, Any]) -> None:
    tasks = {task["id"]: task for task in state["tasks"]}
    if len(tasks) != len(state["tasks"]):
        fail("任务 ID 重复")
    for task in tasks.values():
        if task["status"] == "superseded":
            children = task.get("replaced_by", [])
            if not children or any(child not in tasks or tasks[child].get("replaces") != task["id"] for child in children):
                fail(f"已重拆任务 {task['id']} 缺少完整替代子任务，不能认定完成")
        for dependency in task["depends_on"]:
            if dependency not in tasks:
                fail(f"任务 {task['id']} 引用了不存在的依赖 {dependency}")
            if dependency == task["id"]:
                fail(f"任务 {task['id']} 不能依赖自己")
            if tasks[dependency]["status"] == "superseded":
                fail(f"任务 {task['id']} 仍依赖已重拆任务 {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            fail(f"任务依赖存在环：{task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def role_settings(state: dict[str, Any], role: str) -> dict[str, str]:
    snapshot = state.get("config_snapshot") or {}
    models = snapshot.get("models") if isinstance(snapshot, dict) else None
    model = models.get(role) if isinstance(models, dict) else None
    if isinstance(model, dict):
        model_id = model.get("id")
        effort = model.get("reasoning_effort")
        if isinstance(model_id, str) and isinstance(effort, str):
            return {"model": model_id, "reasoning_effort": effort}
    fallbacks = {
        "implementation": {"model": "inherit", "reasoning_effort": "inherit"},
        "review": {"model": "inherit", "reasoning_effort": "inherit"},
        "qa": {"model": "gpt-6-astra", "reasoning_effort": "high"},
    }
    return fallbacks[role]


def qa_defaults(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = state.get("config_snapshot") or {}
    qa = snapshot.get("qa") if isinstance(snapshot, dict) else None
    return qa if isinstance(qa, dict) else {}


def validate_runtime_record(
    state: dict[str, Any],
    output_dir: Path,
    role: str,
    model: str,
    reasoning_effort: str,
) -> None:
    fingerprint = state.get("config_sha256", "")
    if not fingerprint:
        return
    path = output_dir / "runtime-config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"输出缺少运行时配置记录：{path}")
    except json.JSONDecodeError as exc:
        fail(f"运行时配置记录不是合法 JSON：{exc}")
    expected = {
        "role": role,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "config_fingerprint": fingerprint,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            fail(f"运行时配置 {key} 不匹配：期望 {expected_value!r}")


def resolve_qa_required(state: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.qa_required:
        return True
    if args.no_qa:
        return False
    snapshot = state.get("config_snapshot") or {}
    qa = snapshot.get("qa") if isinstance(snapshot, dict) else None
    mode = qa.get("mode") if isinstance(qa, dict) else "explicit_only"
    if mode == "ui_tasks":
        if args.task_kind == "unspecified":
            fail("qa.mode=ui_tasks 时必须用 --task-kind ui|non-ui 明确任务类型")
        return args.task_kind == "ui"
    if mode == "ask_each_run":
        fail("qa.mode=ask_each_run 时必须显式指定 --qa-required 或 --no-qa")
    return False


def command_init(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    if path.exists():
        fail(f"状态文件已存在，不会覆盖：{path}")
    parse_deadline(args.deadline)
    config: dict[str, Any] = {}
    config_sha256 = ""
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        try:
            config = load_loop_config_json(config_path, "运行配置快照")
            validate_config(config, allow_pending=False)
        except ConfigError as exc:
            fail(f"运行配置快照无效：{exc}")
        config_sha256 = config_fingerprint(config)
    attempts = config.get("attempts", {})
    max_retries = (
        args.max_retries
        if args.max_retries is not None
        else attempts.get("implementation_retries", 2)
    )
    max_review_attempts = (
        args.max_review_attempts
        if args.max_review_attempts is not None
        else attempts.get("review_max_attempts", 5)
    )
    max_qa_attempts = (
        args.max_qa_attempts
        if args.max_qa_attempts is not None
        else attempts.get("qa_max_attempts", 5)
    )
    state = {
        "schema_version": 1,
        "objective": args.objective,
        "status": "active",
        "worktree": str(Path(args.worktree).expanduser().resolve()),
        "branch": args.branch,
        "base_branch": args.base_branch,
        "max_retries_per_task": max_retries,
        "max_review_attempts_per_task": max_review_attempts,
        "max_qa_attempts_per_task": max_qa_attempts,
        "max_replans": args.max_replans,
        "replan_count": 0,
        "replans": [],
        "checkpoint_mode": args.checkpoint_mode,
        "config_snapshot": config,
        "config_sha256": config_sha256,
        "deadline": args.deadline,
        "created_at": now(),
        "updated_at": now(),
        "tasks": [],
        "history": [],
    }
    save_state(path, state)
    print(path)


def new_task(
    task_id: str, title: str, packet: str, depends_on: list[str],
    task_kind: str, qa_required: bool,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "packet": packet,
        "depends_on": depends_on,
        "status": "pending",
        "attempts": 0,
        "sessions": [],
        "runs": [],
        "review_attempts": 0,
        "review_sessions": [],
        "reviews": [],
        "task_kind": task_kind,
        "qa_required": qa_required,
        "qa_attempts": 0,
        "qa_sessions": [],
        "qa_runs": [],
        "qa_audit_decision": "",
        "qa_audit_result": "",
        "commit": "",
        "blocked_artifact": "",
        "parked": False,
        "checkpoint_artifact": "",
        "replaces": "",
        "replaced_by": [],
        "replan_pending": False,
        "stash_ref": "",
        "last_note": "",
        "created_at": now(),
        "updated_at": now(),
    }


def command_add(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    validate_task_id(args.id)
    if not args.title.strip():
        fail("任务标题不能为空")
    if state["checkpoint_mode"] == "patch" and state["tasks"]:
        fail("patch 模式只允许单任务运行；连续多任务必须在启动时选择 commit 模式")
    if any(task["id"] == args.id for task in state["tasks"]):
        fail(f"任务已存在：{args.id}")
    packet = Path(args.packet).expanduser().resolve()
    if not packet.is_file():
        fail(f"任务包不存在：{packet}")
    qa_required = resolve_qa_required(state, args)
    task = new_task(args.id, args.title, str(packet), args.depends_on, args.task_kind, qa_required)
    state["tasks"].append(task)
    add_history(state, task, "added")
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(task, ensure_ascii=False))


def command_validate(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser().resolve())
    validate_graph(state)
    print(f"有效：{len(state['tasks'])} 个任务，无缺失依赖或依赖环")


def command_split(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    validate_graph(state)
    parent = task_by_id(state, args.id)
    if state["checkpoint_mode"] != "commit":
        fail("patch 模式只允许单任务，不能 split")
    if state["replan_count"] >= state["max_replans"]:
        fail("本轮重拆预算已耗尽；保留 blocked 并继续其他可执行任务")
    if parent["status"] != "blocked" or not parent.get("parked"):
        fail("只能重拆已保存现场并安全停放的 blocked 任务")
    if any(task["status"] in ACTIVE_STATUSES for task in state["tasks"]):
        fail("重拆前必须结束或停放所有活动任务")
    if not args.note.strip():
        fail("重拆必须通过 --note 记录过大/无进展原因与拆分依据")
    artifact, artifact_sha256 = require_artifact(state, parent.get("blocked_artifact", ""))
    if parent.get("blocked_artifact_sha256") and parent["blocked_artifact_sha256"] != artifact_sha256:
        fail("已停放产物发生变化；必须重新记录 blocked 产物")
    require_clean_worktree(state)
    plan_path = Path(args.plan).expanduser().resolve()
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"无法读取重拆计划：{exc}")
    if not isinstance(plan, dict) or set(plan) != {"tasks"} or not isinstance(plan["tasks"], list):
        fail('重拆计划格式必须为 {"tasks": [{id, title, packet, task_kind, depends_on}, ...]}')
    if len(plan["tasks"]) < 2:
        fail("重拆至少需要两个更小的子任务；不能仅换 ID 重置预算")
    known_ids = {task["id"] for task in state["tasks"]}
    child_ids: list[str] = []
    packets: set[str] = {parent["packet"]}
    children = []
    for entry in plan["tasks"]:
        if not isinstance(entry, dict) or set(entry) - {"id", "title", "packet", "task_kind", "depends_on"}:
            fail("子任务含未知字段；QA 和预算必须继承，不能通过计划覆盖")
        child_id = entry.get("id")
        validate_task_id(child_id)
        if child_id in known_ids or child_id in child_ids:
            fail(f"子任务 ID 已存在：{child_id}")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            fail("子任务 title 必须是非空字符串")
        packet_value = entry.get("packet")
        if not isinstance(packet_value, str) or not packet_value.strip():
            fail("子任务 packet 必须是实际任务包路径")
        packet = Path(packet_value).expanduser()
        packet = (plan_path.parent / packet).resolve() if not packet.is_absolute() else packet.resolve()
        if not packet.is_file() or not packet.stat().st_size:
            fail(f"子任务包不存在或为空：{packet}")
        if str(packet) in packets:
            fail("每个子任务必须有独立的新任务包，不能复用原任务包")
        packets.add(str(packet))
        kind = entry.get("task_kind", parent["task_kind"])
        if kind not in {"ui", "non-ui", "unspecified"}:
            fail("子任务 task_kind 必须是 ui、non-ui 或 unspecified")
        dependencies = entry.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            fail("子任务 depends_on 必须是任务 ID 字符串数组")
        if parent["id"] in dependencies:
            fail("子任务不能依赖被替代的原任务")
        inherited = list(dict.fromkeys(parent["depends_on"] + dependencies))
        qa_required = parent["qa_required"] or (kind == "ui" and qa_defaults(state).get("mode") == "ui_tasks")
        child = new_task(child_id, title, str(packet), inherited, kind, qa_required)
        child["replaces"] = parent["id"]
        children.append(child)
        child_ids.append(child_id)
    # 在一次原子保存中添加子任务、改写所有外部依赖并保留原任务审计记录。
    for task in state["tasks"]:
        if parent["id"] in task["depends_on"]:
            task["depends_on"] = list(dict.fromkeys(
                dep for original in task["depends_on"]
                for dep in (child_ids if original == parent["id"] else [original])
            ))
    parent["status"] = "superseded"
    parent["replan_pending"] = False
    parent["replaced_by"] = child_ids
    parent["updated_at"] = now()
    parent["last_note"] = args.note
    state["tasks"].extend(children)
    validate_graph(state)
    state["replan_count"] += 1
    state["replans"].append({"at": now(), "parent": parent["id"], "children": child_ids,
                             "plan_path": str(plan_path), "plan": plan, "note": args.note,
                             "blocked_artifact": artifact, "artifact_sha256": artifact_sha256})
    add_history(state, parent, "blocked->superseded", args.note)
    for child in children:
        add_history(state, child, "added:split", parent["id"])
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps({"parent": parent["id"], "children": child_ids,
                      "replan_count": state["replan_count"]}, ensure_ascii=False))


def command_next(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser().resolve())
    validate_graph(state)
    deadline = parse_deadline(state["deadline"])
    if deadline is not None and datetime.now(timezone.utc) >= deadline:
        print(json.dumps({"result": "deadline", "deadline": state["deadline"]}, ensure_ascii=False))
        return
    active = [task for task in state["tasks"] if task["status"] in ACTIVE_STATUSES]
    # 先恢复当前 worker；不能为了别的任务停放或重拆而干扰它正在写入的工作区。
    for task in active or state["tasks"]:
        reason = park_reason(state, task)
        if reason:
            action = ("重拆预算耗尽；普通 set blocked --note 显式放弃重拆意图，然后再次 next"
                      if reason == "replan_budget_exhausted" else
                      "保存现场并停止相关进程，安全停放改动后 set blocked --artifact；如需继续重拆保留 --replan；然后再次 next")
            print(json.dumps({"result": "park", "task": task, "reason": reason,
                              "action": action},
                             ensure_ascii=False, indent=2))
            return
    if active:
        print(json.dumps({"result": "recover", "tasks": active}, ensure_ascii=False, indent=2))
        return
    for task in state["tasks"]:
        if task["status"] == "blocked" and task.get("replan_pending"):
            print(json.dumps({"result": "replan", "task": task,
                              "action": "按原目标与授权细分任务并执行 split；若无法继续拆分，set blocked --note 显式放弃重拆意图"},
                             ensure_ascii=False, indent=2))
            return
    statuses = {task["id"]: task["status"] for task in state["tasks"]}
    ready = [
        task
        for task in state["tasks"]
        if task["status"] == "pending"
        and all(statuses[dependency] == "done" for dependency in task["depends_on"])
    ]
    if ready:
        print(json.dumps({"result": "ready", "task": ready[0]}, ensure_ascii=False, indent=2))
    elif state["tasks"] and all(task["status"] in TERMINAL_STATUSES for task in state["tasks"]):
        print(json.dumps({"result": "complete"}, ensure_ascii=False))
    else:
        blocked = [task for task in state["tasks"] if task["status"] not in TERMINAL_STATUSES]
        print(json.dumps({"result": "blocked", "tasks": blocked}, ensure_ascii=False, indent=2))


def command_start(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    validate_graph(state)
    task = task_by_id(state, args.id)
    if task["status"] not in {"pending", "retrying"}:
        fail(f"任务 {args.id} 不能从 {task['status']} 开始执行")
    if any(other["id"] != args.id and other["status"] in ACTIVE_STATUSES for other in state["tasks"]):
        fail("其他任务仍在活动中；先恢复或停放该任务")
    if any(task_by_id(state, dep)["status"] != "done" for dep in task["depends_on"]):
        fail(f"任务 {args.id} 的前置依赖尚未完成")
    if any(other["status"] == "blocked" and not other.get("parked") for other in state["tasks"]):
        fail("存在尚未保存并停放的 blocked 任务")
    if task["status"] == "pending" and (state["checkpoint_mode"] == "commit" or task["attempts"] == 0):
        require_clean_worktree(state)
    allowed_attempts = state["max_retries_per_task"] + 1
    if task["attempts"] >= allowed_attempts:
        fail(f"任务 {args.id} 已达到最多 {allowed_attempts} 次执行，必须标记 blocked")
    output_dir = str(Path(args.output_dir).expanduser().resolve())
    if any(run["output_dir"] == output_dir for run in task["runs"]):
        fail(f"输出目录已经登记过：{output_dir}")
    if args.resume_session:
        if task["status"] != "retrying":
            fail("只有 retrying 任务可以使用 --resume-session")
        if not any(item["id"] == args.resume_session for item in task["sessions"]):
            fail(f"不能 resume 未登记的 session：{args.resume_session}")
    old_status = task["status"]
    start_head = git(state, "rev-parse", "--verify", "HEAD")
    task["status"] = "running"
    task["parked"] = False
    task["attempts"] += 1
    settings = role_settings(state, "implementation")
    task["runs"].append(
        {
            "attempt": task["attempts"],
            "output_dir": output_dir,
            "started_at": now(),
            "status": "running",
            "session_id": args.resume_session,
            "session_kind": "resumed" if args.resume_session else "",
            "start_head": start_head,
            **settings,
        }
    )
    task["updated_at"] = now()
    add_history(state, task, f"{old_status}->running", args.note)
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(task, ensure_ascii=False))


def command_session(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "running":
        fail(f"只有 running 任务可以登记 session，当前为 {task['status']}")
    if args.session_id in state_session_ids(state):
        fail(f"session 已登记；新任务和新角色必须使用全新 session：{args.session_id}")
    expected_kind = "initial" if task["attempts"] == 1 else "replacement"
    if args.kind != expected_kind:
        fail(f"任务 {args.id} 的下一个 session 必须登记为 {expected_kind}")
    if not task["runs"] or task["runs"][-1]["status"] != "running":
        fail(f"任务 {args.id} 没有可登记 session 的运行轮次")
    task["sessions"].append({"id": args.session_id, "kind": args.kind, "at": now()})
    task["runs"][-1]["session_id"] = args.session_id
    task["runs"][-1]["session_kind"] = args.kind
    task["updated_at"] = now()
    add_history(state, task, f"session:{args.kind}", args.session_id)
    save_state(path, state)
    print(json.dumps(task["sessions"][-1], ensure_ascii=False))


def command_review_start(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "reviewing":
        fail(f"只有 reviewing 任务可以启动 reviewer，当前为 {task['status']}")
    if task["reviews"] and task["reviews"][-1]["status"] == "running":
        fail(f"任务 {args.id} 已有 reviewer 在运行")
    if task["review_attempts"] >= state["max_review_attempts_per_task"]:
        fail(
            f"任务 {args.id} 已达到最多 {state['max_review_attempts_per_task']} 次 review，"
            "必须仲裁后标记 blocked 或进入 verifying"
        )
    output_dir = str(Path(args.output_dir).expanduser().resolve())
    if any(review["output_dir"] == output_dir for review in task["reviews"]):
        fail(f"review 输出目录已经登记过：{output_dir}")
    task["review_attempts"] += 1
    settings = role_settings(state, "review")
    review = {
        "attempt": task["review_attempts"],
        "output_dir": output_dir,
        "target": args.target,
        "started_at": now(),
        "finished_at": "",
        "status": "running",
        "session_id": "",
        "reviewer_verdict": "",
        "decision": "",
        "result_file": "",
        "target_fingerprint": "",
        "note": args.note,
        **settings,
    }
    task["reviews"].append(review)
    task["updated_at"] = now()
    add_history(state, task, "review:start", args.note)
    save_state(path, state)
    print(json.dumps(review, ensure_ascii=False))


def command_review_session(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "reviewing":
        fail(f"只有 reviewing 任务可以登记 reviewer session，当前为 {task['status']}")
    if not task["reviews"] or task["reviews"][-1]["status"] != "running":
        fail(f"任务 {args.id} 没有正在运行的 review")
    session_file = Path(task["reviews"][-1]["output_dir"]) / "session-id.txt"
    if not session_file.is_file():
        fail(f"review session 文件不存在：{session_file}")
    recorded_session = session_file.read_text(encoding="utf-8").splitlines()
    expected_session = recorded_session[0].strip() if recorded_session else ""
    if not expected_session or expected_session != args.session_id:
        fail("登记的 reviewer session 与输出目录中的 session-id.txt 不一致")
    if args.session_id in state_session_ids(state):
        fail(f"session 已登记，reviewer 必须使用全新 session：{args.session_id}")
    record = {"id": args.session_id, "kind": "review", "at": now()}
    task["review_sessions"].append(record)
    task["reviews"][-1]["session_id"] = args.session_id
    task["updated_at"] = now()
    add_history(state, task, "review:session", args.session_id)
    save_state(path, state)
    print(json.dumps(record, ensure_ascii=False))


def load_review_result(
    state: dict[str, Any], path_value: str, review: dict[str, Any]
) -> tuple[str, str, str]:
    result_path = Path(path_value).expanduser().resolve()
    expected_path = (Path(review["output_dir"]) / "review.json").resolve()
    if result_path != expected_path:
        fail(f"review 结果必须来自当前 review 输出目录：{expected_path}")
    if not result_path.is_file():
        fail(f"review 结果不存在：{result_path}")
    validate_runtime_record(
        state,
        expected_path.parent,
        "review",
        review["model"],
        review["reasoning_effort"],
    )
    watchdog_path = expected_path.parent / "watchdog.json"
    before_path = expected_path.parent / "target-before.json"
    after_path = expected_path.parent / "target-after.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        watchdog = json.loads(watchdog_path.read_text(encoding="utf-8"))
        before = json.loads(before_path.read_text(encoding="utf-8"))
        after = json.loads(after_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        fail(f"review 输出缺少必要文件：{exc.filename}")
    except json.JSONDecodeError as exc:
        fail(f"review 输出不是合法 JSON：{exc}")
    if watchdog.get("result") != "completed" or watchdog.get("exit_code") != 0:
        fail("review watchdog 未成功完成，不能仲裁结果")
    fingerprint = before.get("fingerprint")
    if not fingerprint or fingerprint != after.get("fingerprint"):
        fail("review 前后目标 fingerprint 不一致，结果已过期")
    verdict = result.get("verdict")
    findings = result.get("findings")
    if verdict not in {"pass", "changes_requested", "blocked"} or not isinstance(findings, list):
        fail("review 结果缺少合法 verdict/findings")
    if verdict == "pass" and findings:
        fail("review verdict=pass 时 findings 必须为空")
    if verdict == "changes_requested" and not findings:
        fail("review verdict=changes_requested 时 findings 不能为空")
    return verdict, str(result_path), fingerprint


def command_review_finish(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "reviewing":
        fail(f"只有 reviewing 任务可以结束 review，当前为 {task['status']}")
    if not task["reviews"] or task["reviews"][-1]["status"] != "running":
        fail(f"任务 {args.id} 没有正在运行的 review")
    review = task["reviews"][-1]
    if args.decision == "error":
        if not args.note:
            fail("review 基础设施失败时必须提供 --note")
        verdict, result_file, target_fingerprint = "", "", ""
        next_status = "reviewing"
        review_status = "error"
    else:
        if not review["session_id"]:
            fail("review 完成前必须登记 reviewer session")
        if not args.result:
            fail("review 仲裁必须提供 --result")
        verdict, result_file, target_fingerprint = load_review_result(state, args.result, review)
        next_status = {
            "accept": "verifying",
            "retry": "retrying",
            "blocked": "blocked",
        }[args.decision]
        review_status = "completed"
        expected = {
            "pass": "accept",
            "changes_requested": "retry",
            "blocked": "blocked",
        }[verdict]
        if args.decision != expected and not args.note:
            fail(
                f"主 agent 的决定 {args.decision} 与 reviewer verdict {verdict} 不一致，"
                "必须用 --note 记录仲裁理由"
            )
        if next_status in {"retrying", "blocked"} and not args.note:
            fail(f"review 决定为 {args.decision} 时必须提供 --note")
    old_status = task["status"]
    task["status"] = next_status
    if next_status == "blocked":
        task["parked"] = False
    task["last_note"] = args.note
    if next_status in {"retrying", "blocked"} and task["runs"]:
        task["runs"][-1]["status"] = "needs-retry" if next_status == "retrying" else "blocked"
        task["runs"][-1]["finished_at"] = now()
    review.update(
        {
            "finished_at": now(),
            "status": review_status,
            "reviewer_verdict": verdict,
            "decision": args.decision,
            "result_file": result_file,
            "target_fingerprint": target_fingerprint,
            "note": args.note,
        }
    )
    task["updated_at"] = now()
    add_history(state, task, f"{old_status}->{next_status}:review-{args.decision}", args.note)
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(review, ensure_ascii=False))


def all_session_ids(task: dict[str, Any]) -> set[str]:
    return {
        item["id"]
        for item in task["sessions"] + task["review_sessions"] + task["qa_sessions"]
    }


def state_session_ids(state: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for task in state["tasks"]:
        result.update(all_session_ids(task))
    return result


def command_qa_start(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "verifying":
        fail(f"只有 verifying 任务可以启动 UI QA，当前为 {task['status']}")
    if not task["qa_required"]:
        fail(f"任务 {args.id} 未标记 --qa-required")
    settings = role_settings(state, "qa")
    defaults = qa_defaults(state)
    model = args.model or settings["model"]
    reasoning_effort = args.reasoning_effort or settings["reasoning_effort"]
    driver = args.driver or defaults.get("driver") or ""
    environment = args.environment or defaults.get("default_environment") or ""
    if not all(
        isinstance(value, str) and value.strip()
        for value in (model, reasoning_effort, driver, environment)
    ):
        fail("QA model、reasoning effort、driver 和 environment 都必须是非空值")
    if task["qa_runs"] and task["qa_runs"][-1]["status"] == "running":
        fail(f"任务 {args.id} 已有 QA session 在运行")
    if task["qa_attempts"] >= state["max_qa_attempts_per_task"]:
        fail(
            f"任务 {args.id} 已达到最多 {state['max_qa_attempts_per_task']} 次 QA，"
            "必须标记 blocked 或由用户扩大预算"
        )
    output_dir = Path(args.output_dir).expanduser().resolve()
    worktree = Path(state["worktree"]).resolve()
    try:
        output_dir.relative_to(worktree)
    except ValueError:
        pass
    else:
        fail("QA 输出目录必须位于 worktree 之外")
    if any(Path(run["output_dir"]).resolve() == output_dir for run in task["qa_runs"]):
        fail(f"QA 输出目录已经登记过：{output_dir}")
    task["qa_attempts"] += 1
    task["qa_audit_decision"] = ""
    task["qa_audit_result"] = ""
    qa_run = {
        "attempt": task["qa_attempts"],
        "output_dir": str(output_dir),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "driver": driver,
        "environment": environment,
        "started_at": now(),
        "finished_at": "",
        "status": "running",
        "session_id": "",
        "verdict": "",
        "result_file": "",
        "target_fingerprint": "",
        "evidence_fingerprint": "",
        "audit_decision": "",
        "audit_result_file": "",
        "audit_validation": {},
        "note": args.note,
    }
    old_status = task["status"]
    task["status"] = "qa_running"
    task["qa_runs"].append(qa_run)
    task["updated_at"] = now()
    add_history(state, task, f"{old_status}->qa_running", args.note)
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(qa_run, ensure_ascii=False))


def command_qa_session(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "qa_running":
        fail(f"只有 qa_running 任务可以登记 QA session，当前为 {task['status']}")
    if not task["qa_runs"] or task["qa_runs"][-1]["status"] != "running":
        fail(f"任务 {args.id} 没有正在运行的 QA")
    qa_run = task["qa_runs"][-1]
    session_file = Path(qa_run["output_dir"]) / "session-id.txt"
    if not session_file.is_file():
        fail(f"QA session 文件不存在：{session_file}")
    recorded = session_file.read_text(encoding="utf-8").splitlines()
    expected = recorded[0].strip() if recorded else ""
    if not expected or expected != args.session_id:
        fail("登记的 QA session 与输出目录中的 session-id.txt 不一致")
    if args.session_id in state_session_ids(state):
        fail(f"session 已登记，QA 必须使用全新 session：{args.session_id}")
    record = {"id": args.session_id, "kind": "qa", "at": now()}
    task["qa_sessions"].append(record)
    qa_run["session_id"] = args.session_id
    task["updated_at"] = now()
    add_history(state, task, "qa:session", args.session_id)
    save_state(path, state)
    print(json.dumps(record, ensure_ascii=False))


def load_qa_result(
    state: dict[str, Any], qa_run: dict[str, Any], result_value: str
) -> dict[str, Any]:
    output_dir = Path(qa_run["output_dir"]).resolve()
    result_path = Path(result_value).expanduser().resolve()
    expected_path = output_dir / "qa-result.json"
    if result_path != expected_path:
        fail(f"QA 结果必须来自当前 QA 输出目录：{expected_path}")
    validate_runtime_record(
        state,
        output_dir,
        "qa",
        qa_run["model"],
        qa_run["reasoning_effort"],
    )
    paths = {
        "watchdog": output_dir / "watchdog.json",
        "before": output_dir / "target-before.json",
        "after": output_dir / "target-after.json",
        "validation": output_dir / "validation.json",
    }
    try:
        watchdog = json.loads(paths["watchdog"].read_text(encoding="utf-8"))
        before = json.loads(paths["before"].read_text(encoding="utf-8"))
        after = json.loads(paths["after"].read_text(encoding="utf-8"))
        recorded_validation = json.loads(paths["validation"].read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        fail(f"QA 输出缺少必要文件：{exc.filename}")
    except json.JSONDecodeError as exc:
        fail(f"QA 输出不是合法 JSON：{exc}")
    if watchdog.get("result") != "completed" or watchdog.get("exit_code") != 0:
        fail("QA watchdog 未成功完成，不能进入审计")
    fingerprint = before.get("fingerprint")
    if not fingerprint or fingerprint != after.get("fingerprint"):
        fail("QA 前后 worktree fingerprint 不一致，结果已过期")
    try:
        validation = validate_run(
            result_path,
            output_dir / "evidence",
            qa_run["model"],
            qa_run["reasoning_effort"],
            qa_run["driver"],
            qa_run["environment"],
            fingerprint,
        )
    except ValidationError as exc:
        fail(f"QA 证据验证失败：{exc}")
    if recorded_validation != validation:
        fail("QA validation 与当前结果或证据不一致")
    return {"path": str(result_path), "validation": validation}


def command_qa_finish(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "qa_running":
        fail(f"只有 qa_running 任务可以结束 QA，当前为 {task['status']}")
    if not task["qa_runs"] or task["qa_runs"][-1]["status"] != "running":
        fail(f"任务 {args.id} 没有正在运行的 QA")
    qa_run = task["qa_runs"][-1]
    old_status = task["status"]
    if args.decision == "error":
        if not args.note:
            fail("QA 基础设施失败时必须提供 --note")
        next_status = "verifying"
        qa_run.update({"status": "error", "finished_at": now(), "note": args.note})
    else:
        if not qa_run["session_id"]:
            fail("QA 完成前必须登记全新 QA session")
        if not args.result:
            fail("QA 完成时必须提供 --result")
        loaded = load_qa_result(state, qa_run, args.result)
        validation = loaded["validation"]
        next_status = "qa_auditing"
        qa_run.update(
            {
                "status": "completed",
                "finished_at": now(),
                "verdict": validation["verdict"],
                "result_file": loaded["path"],
                "target_fingerprint": validation["target_fingerprint"],
                "evidence_fingerprint": validation["evidence_fingerprint"],
                "note": args.note,
            }
        )
    task["status"] = next_status
    task["last_note"] = args.note
    task["updated_at"] = now()
    add_history(state, task, f"{old_status}->{next_status}:qa-{args.decision}", args.note)
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(qa_run, ensure_ascii=False))


def command_qa_audit(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    if task["status"] != "qa_auditing":
        fail(f"只有 qa_auditing 任务可以记录 CC 复测，当前为 {task['status']}")
    if not task["qa_runs"] or task["qa_runs"][-1]["status"] != "completed":
        fail(f"任务 {args.id} 没有可审计的 QA 结果")
    qa_run = task["qa_runs"][-1]
    output_dir = Path(qa_run["output_dir"]).resolve()
    # 审计可能发生在 QA 完成很久以后；先重新计算结果与截图哈希，拒绝被替换的证据。
    refreshed = load_qa_result(state, qa_run, qa_run["result_file"])["validation"]
    if refreshed["evidence_fingerprint"] != qa_run["evidence_fingerprint"]:
        fail("Codex QA 证据在进入 CC 审计后发生变化")
    audit_path = Path(args.result).expanduser().resolve()
    expected_path = output_dir / "cc-audit.json"
    if audit_path != expected_path:
        fail(f"CC QA 审计必须写入当前 QA 输出目录：{expected_path}")
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        validation = validate_audit(
            audit_path,
            output_dir / "cc-audit-evidence",
            output_dir / "validation.json",
        )
    except FileNotFoundError as exc:
        fail(f"CC QA 审计缺少必要文件：{exc.filename}")
    except json.JSONDecodeError as exc:
        fail(f"CC QA 审计不是合法 JSON：{exc}")
    except ValidationError as exc:
        fail(f"CC QA 审计验证失败：{exc}")
    if audit.get("decision") != args.decision:
        fail("--decision 与 cc-audit.json 不一致")
    expected = {"pass": "accept", "fail": "retry", "blocked": "blocked"}[qa_run["verdict"]]
    if args.decision != expected and not args.note:
        fail(
            f"CC 决定 {args.decision} 与 Codex QA verdict {qa_run['verdict']} 不一致，"
            "必须用 --note 记录独立复测证据"
        )
    if args.decision in {"retry", "blocked"} and not args.note:
        fail(f"CC QA 决定为 {args.decision} 时必须提供 --note")

    qa_run["audit_decision"] = args.decision
    qa_run["audit_result_file"] = str(audit_path)
    qa_run["audit_validation"] = validation
    task["qa_audit_decision"] = args.decision
    task["qa_audit_result"] = str(audit_path)
    task["last_note"] = args.note
    if args.decision == "retry":
        task["status"] = "retrying"
        if task["runs"]:
            task["runs"][-1]["status"] = "needs-retry"
            task["runs"][-1]["finished_at"] = now()
    elif args.decision == "blocked":
        task["status"] = "blocked"
        task["parked"] = False
        if task["runs"]:
            task["runs"][-1]["status"] = "blocked"
            task["runs"][-1]["finished_at"] = now()
    task["updated_at"] = now()
    add_history(state, task, f"qa-audit:{args.decision}", args.note)
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(qa_run, ensure_ascii=False))


def command_set(args: argparse.Namespace) -> None:
    path = Path(args.state).expanduser().resolve()
    state = load_state(path)
    task = task_by_id(state, args.id)
    target = args.status
    if args.replan:
        if target != "blocked":
            fail("--replan 只能用于 set blocked，表示保存现场后继续拆分")
        if state["checkpoint_mode"] != "commit":
            fail("patch 模式只允许单任务，不能登记重拆意图")
        if state["replan_count"] >= state["max_replans"]:
            fail("本轮重拆预算已耗尽，不能登记 --replan")
    if target not in TRANSITIONS[task["status"]]:
        fail(f"非法状态迁移：{task['status']} -> {target}")
    if target in {"retrying", "blocked"} and not args.note:
        fail(f"迁移到 {target} 时必须提供 --note")
    if task["status"] == "reviewing" and target == "verifying" and not args.note:
        fail("绕过结构化 reviewer 直接进入 verifying 时必须用 --note 记录人工仲裁理由")
    if task["status"] == "qa_running" and target == "verifying" and not args.note:
        fail("绕过 qa-finish 返回 verifying 时必须用 --note 记录基础设施失败原因")
    artifact = ""
    artifact_sha256 = ""
    checkpoint_commit = ""
    if args.artifact:
        artifact, artifact_sha256 = require_artifact(state, args.artifact)
    if target == "blocked":
        if artifact and state["checkpoint_mode"] == "commit":
            require_clean_worktree(state)
        task["parked"] = bool(artifact) or task["attempts"] == 0 or (task["status"] == "blocked" and task.get("parked", False))
        task["replan_pending"] = args.replan
    if target == "pending":
        if not task.get("parked"):
            fail("blocked 任务必须先保存并停放现场才能重开")
        if task["attempts"] >= state["max_retries_per_task"] + 1:
            fail("实现预算耗尽，不能重开同一任务；可在重拆预算内 split，或继续其他任务")
        task["parked"] = False
        task["replan_pending"] = False
    if target == "done":
        if task["qa_required"]:
            if task["status"] != "qa_auditing" or task["qa_audit_decision"] != "accept":
                fail("需要 UI QA 的任务只有在 Codex QA 和 CC 独立复测均通过后才能 done")
        elif task["status"] != "verifying":
            fail("不需要 UI QA 的任务只能从 verifying 进入 done")
        if state["checkpoint_mode"] == "commit":
            if not args.commit:
                fail("commit 模式 done 必须提供 --commit，并且对应当前干净 worktree 的 HEAD")
            require_clean_worktree(state)
            checkpoint_commit = git(state, "rev-parse", "--verify", "--end-of-options", args.commit + "^{commit}")
            head = git(state, "rev-parse", "--verify", "HEAD")
            if checkpoint_commit != head:
                fail("--commit 必须解析为当前 HEAD，不能登记历史 checkpoint")
            start_head = task["runs"][0].get("start_head") if task["runs"] else None
            if start_head == head and not args.note.strip():
                fail("HEAD 与任务开始时相同；无代码变更的任务必须用 --note 说明验收结果")
        else:
            if args.commit:
                fail("patch 模式禁止登记 commit；本轮仅交付单任务产物")
            if not artifact:
                fail("patch 模式 done 必须通过 --artifact 提供非空的补丁/归档验收产物")
            if len(state["tasks"]) != 1:
                fail("patch 模式只能完成单任务运行")
    if target == "reviewing":
        if not task["runs"] or task["runs"][-1]["status"] != "running":
            fail("进入 reviewing 前必须有正在运行的实现轮次")
        run = task["runs"][-1]
        validate_runtime_record(
            state,
            Path(run["output_dir"]),
            "implementation",
            run["model"],
            run["reasoning_effort"],
        )
        if not run.get("session_id"):
            fail("进入 reviewing 前必须登记实现 session")
        output_dir = Path(run["output_dir"])
        watchdog_path = output_dir / "watchdog.json"
        report_path = output_dir / "report.md"
        try:
            watchdog = json.loads(watchdog_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            fail(f"实现输出缺少 watchdog：{watchdog_path}")
        except json.JSONDecodeError as exc:
            fail(f"实现 watchdog 不是合法 JSON：{exc}")
        if watchdog.get("result") != "completed" or watchdog.get("exit_code") != 0:
            fail("实现 watchdog 未成功完成，不能进入 reviewing")
        if not report_path.is_file() or report_path.stat().st_size == 0:
            fail(f"实现输出缺少完成报告：{report_path}")
    old_status = task["status"]
    task["status"] = target
    task["last_note"] = args.note
    if task["runs"]:
        run_status = {
            "reviewing": "implemented",
            "verifying": "completed",
            "retrying": "needs-retry",
            "done": "verified",
            "blocked": "blocked",
            "pending": "reset",
        }[target]
        task["runs"][-1]["status"] = run_status
        task["runs"][-1]["finished_at"] = now()
    if args.commit:
        task["commit"] = checkpoint_commit or args.commit
    if artifact:
        key = "checkpoint_artifact" if target == "done" else "blocked_artifact"
        task[key] = artifact
        task[key + "_sha256"] = artifact_sha256
    if args.stash_ref:
        task["stash_ref"] = args.stash_ref
    task["updated_at"] = now()
    add_history(state, task, f"{old_status}->{target}", args.note)
    refresh_run_status(state)
    save_state(path, state)
    print(json.dumps(task, ensure_ascii=False))


def command_summary(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser().resolve())
    validate_graph(state)
    refresh_run_status(state)
    counts = {status: 0 for status in sorted(STATUSES)}
    for task in state["tasks"]:
        counts[task["status"]] += 1
    output = {
        "objective": state["objective"],
        "status": state["status"],
        "branch": state["branch"],
        "worktree": state["worktree"],
        "deadline": state["deadline"],
        "config_sha256": state.get("config_sha256", ""),
        "checkpoint_mode": state["checkpoint_mode"],
        "replan_count": state["replan_count"],
        "max_replans": state["max_replans"],
        "models": {
            role: role_settings(state, role)
            for role in ("implementation", "review", "qa")
        },
        "counts": counts,
        "tasks": [
            {
                "id": task["id"],
                "status": task["status"],
                "task_kind": task["task_kind"],
                "attempts": task["attempts"],
                "sessions": len(task["sessions"]),
                "review_attempts": task["review_attempts"],
                "review_sessions": len(task["review_sessions"]),
                "last_review_decision": task["reviews"][-1]["decision"] if task["reviews"] else "",
                "qa_required": task["qa_required"],
                "qa_attempts": task["qa_attempts"],
                "qa_sessions": len(task["qa_sessions"]),
                "last_qa_verdict": task["qa_runs"][-1]["verdict"] if task["qa_runs"] else "",
                "qa_audit_decision": task["qa_audit_decision"],
                "last_output_dir": task["runs"][-1]["output_dir"] if task["runs"] else "",
                "commit": task["commit"],
                "blocked_artifact": task["blocked_artifact"],
                "parked": task["parked"],
                "checkpoint_artifact": task["checkpoint_artifact"],
                "replaces": task["replaces"],
                "replaced_by": task["replaced_by"],
                "replan_pending": task["replan_pending"],
                "stash_ref": task["stash_ref"],
                "last_note": task["last_note"],
            }
            for task in state["tasks"]
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def command_show(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state).expanduser().resolve())
    print(json.dumps(state, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="创建新的运行状态")
    init.add_argument("state")
    init.add_argument("--objective", required=True)
    init.add_argument("--worktree", required=True)
    init.add_argument("--branch", required=True)
    init.add_argument("--base-branch", required=True)
    init.add_argument("--config", help="loop_config.py resolve 生成的冻结配置")
    init.add_argument("--max-retries", type=int)
    init.add_argument("--max-review-attempts", type=int)
    init.add_argument("--max-qa-attempts", type=int)
    init.add_argument("--max-replans", type=int, default=2, help="整轮运行允许的重拆次数")
    init.add_argument("--checkpoint-mode", choices=["commit", "patch"], default="commit")
    init.add_argument("--deadline", default="")
    init.set_defaults(func=command_init)

    add = subparsers.add_parser("add", help="添加任务")
    add.add_argument("state")
    add.add_argument("--id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--packet", required=True)
    add.add_argument("--depends-on", nargs="*", default=[])
    add.add_argument("--task-kind", choices=["ui", "non-ui", "unspecified"], default="unspecified")
    qa_choice = add.add_mutually_exclusive_group()
    qa_choice.add_argument("--qa-required", action="store_true")
    qa_choice.add_argument("--no-qa", action="store_true")
    add.set_defaults(func=command_add)

    validate = subparsers.add_parser("validate", help="检查依赖完整性和依赖环")
    validate.add_argument("state")
    validate.set_defaults(func=command_validate)

    split = subparsers.add_parser("split", help="将已停放 blocked 任务替换为更小的全新任务")
    split.add_argument("state")
    split.add_argument("id")
    split.add_argument("--plan", required=True)
    split.add_argument("--note", required=True)
    split.set_defaults(func=command_split)

    next_task = subparsers.add_parser("next", help="选择 ready 任务或指出恢复/阻塞状态")
    next_task.add_argument("state")
    next_task.set_defaults(func=command_next)

    start = subparsers.add_parser("start", help="将 pending/retrying 任务置为 running")
    start.add_argument("state")
    start.add_argument("id")
    start.add_argument("--output-dir", required=True)
    start.add_argument("--resume-session", default="")
    start.add_argument("--note", default="")
    start.set_defaults(func=command_start)

    session = subparsers.add_parser("session", help="给 running 任务登记一个 Codex session")
    session.add_argument("state")
    session.add_argument("id")
    session.add_argument("session_id")
    session.add_argument("--kind", choices=["initial", "replacement"], default="initial")
    session.set_defaults(func=command_session)

    review_start = subparsers.add_parser("review-start", help="给 reviewing 任务启动全新 reviewer")
    review_start.add_argument("state")
    review_start.add_argument("id")
    review_start.add_argument("--output-dir", required=True)
    review_start.add_argument("--target", choices=["uncommitted"], default="uncommitted")
    review_start.add_argument("--note", default="")
    review_start.set_defaults(func=command_review_start)

    review_session = subparsers.add_parser("review-session", help="登记全新 reviewer session")
    review_session.add_argument("state")
    review_session.add_argument("id")
    review_session.add_argument("session_id")
    review_session.set_defaults(func=command_review_session)

    review_finish = subparsers.add_parser("review-finish", help="记录 reviewer 结论和主 agent 仲裁")
    review_finish.add_argument("state")
    review_finish.add_argument("id")
    review_finish.add_argument(
        "--decision", choices=["accept", "retry", "blocked", "error"], required=True
    )
    review_finish.add_argument("--result", default="")
    review_finish.add_argument("--note", default="")
    review_finish.set_defaults(func=command_review_finish)

    qa_start = subparsers.add_parser("qa-start", help="按冻结配置启动全新 UI QA")
    qa_start.add_argument("state")
    qa_start.add_argument("id")
    qa_start.add_argument("--output-dir", required=True)
    qa_start.add_argument("--model")
    qa_start.add_argument("--reasoning-effort")
    qa_start.add_argument("--driver")
    qa_start.add_argument("--environment")
    qa_start.add_argument("--note", default="")
    qa_start.set_defaults(func=command_qa_start)

    qa_session = subparsers.add_parser("qa-session", help="登记全新 QA session")
    qa_session.add_argument("state")
    qa_session.add_argument("id")
    qa_session.add_argument("session_id")
    qa_session.set_defaults(func=command_qa_session)

    qa_finish = subparsers.add_parser("qa-finish", help="记录 Codex QA 证据并进入 CC 审计")
    qa_finish.add_argument("state")
    qa_finish.add_argument("id")
    qa_finish.add_argument("--decision", choices=["audit", "error"], required=True)
    qa_finish.add_argument("--result", default="")
    qa_finish.add_argument("--note", default="")
    qa_finish.set_defaults(func=command_qa_finish)

    qa_audit = subparsers.add_parser("qa-audit", help="记录 Claude Code 截图审计和独立复测")
    qa_audit.add_argument("state")
    qa_audit.add_argument("id")
    qa_audit.add_argument("--decision", choices=["accept", "retry", "blocked"], required=True)
    qa_audit.add_argument("--result", required=True)
    qa_audit.add_argument("--note", default="")
    qa_audit.set_defaults(func=command_qa_audit)

    set_status = subparsers.add_parser("set", help="推进任务状态")
    set_status.add_argument("state")
    set_status.add_argument("id")
    set_status.add_argument(
        "status", choices=sorted(STATUSES - {"running", "qa_running", "qa_auditing", "superseded"})
    )
    set_status.add_argument("--note", default="")
    set_status.add_argument("--commit", default="")
    set_status.add_argument("--artifact", default="")
    set_status.add_argument("--stash-ref", default="")
    set_status.add_argument("--replan", action="store_true", help="blocked 停放后仍需继续有界重拆")
    set_status.set_defaults(func=command_set)

    summary = subparsers.add_parser("summary", help="输出紧凑状态摘要")
    summary.add_argument("state")
    summary.set_defaults(func=command_summary)

    show = subparsers.add_parser("show", help="输出完整状态")
    show.add_argument("state")
    show.set_defaults(func=command_show)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "max_retries", None) is not None and args.max_retries < 0:
        parser.error("--max-retries 必须是非负整数")
    if (
        getattr(args, "max_review_attempts", None) is not None
        and args.max_review_attempts < 1
    ):
        parser.error("--max-review-attempts 必须是正整数")
    if getattr(args, "max_qa_attempts", None) is not None and args.max_qa_attempts < 1:
        parser.error("--max-qa-attempts 必须是正整数")
    if getattr(args, "max_replans", None) is not None and args.max_replans < 0:
        parser.error("--max-replans 必须是非负整数")
    args.func(args)


if __name__ == "__main__":
    main()
