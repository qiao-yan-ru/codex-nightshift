#!/usr/bin/env python3
"""解析、校验并冻结 codex-nightshift 的 JSON 配置。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = SCRIPT_DIR / "config.defaults.json"
DEFAULT_USER_PATH = Path.home() / ".claude" / "codex-nightshift" / "config.json"
PROJECT_RELATIVE_PATH = Path(".claude/codex-nightshift.json")
LEGACY_USER_PATH = Path.home() / ".claude" / "codex-implementation-loop" / "config.json"
LEGACY_PROJECT_RELATIVE_PATH = Path(".claude/codex-implementation-loop.json")
ROLES = ("implementation", "review", "qa")
EFFORTS = {"inherit", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
QA_MODES = {"ui_tasks", "ask_each_run", "explicit_only", "disabled"}
ONBOARDING_STATUSES = {"pending", "complete", "deferred"}
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
KNOWN_MODEL_EFFORTS = {
    "gpt-5.6-sol": {"none", "low", "medium", "high", "xhigh", "max"},
    "gpt-6-astra": {"low", "medium", "high", "xhigh", "max"},
}
FULL_KEYS = {"schema_version", "onboarding", "models", "qa", "attempts", "watchdog"}
PROJECT_KEYS = {"schema_version", "models", "qa", "attempts", "watchdog"}
NESTED_KEYS = {
    "models": set(ROLES),
    "qa": {"mode", "driver", "default_environment"},
    "attempts": {"implementation_retries", "review_max_attempts", "qa_max_attempts"},
    "watchdog": {
        "poll_seconds",
        "idle_timeout_seconds",
        "max_runtime_seconds",
        "terminate_grace_seconds",
    },
}


class ConfigError(ValueError):
    pass


class OnboardingRequired(ConfigError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{label}不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label}不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{label}顶层必须是对象")
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def reject_unknown_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{label}包含未知字段：{', '.join(sorted(unknown))}")


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label}必须是对象")
    return value


def require_int(value: Any, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{label}必须是大于等于 {minimum} 的整数")
    return value


def require_clean_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}必须是非空字符串或 null")
    if any(ord(char) < 32 for char in value):
        raise ConfigError(f"{label}不能包含控制字符")
    return value


def validate_model(value: Any, label: str) -> None:
    model = require_object(value, label)
    reject_unknown_keys(model, {"id", "reasoning_effort"}, label)
    if set(model) != {"id", "reasoning_effort"}:
        raise ConfigError(f"{label}必须同时包含 id 和 reasoning_effort")
    model_id = model["id"]
    effort = model["reasoning_effort"]
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ConfigError(f"{label}.id 格式非法：{model_id!r}")
    if effort not in EFFORTS:
        raise ConfigError(f"{label}.reasoning_effort 非法：{effort!r}")
    if model_id == "inherit" and effort != "inherit":
        raise ConfigError(f"{label} 使用 inherit 模型时 reasoning_effort 也必须是 inherit")
    supported = KNOWN_MODEL_EFFORTS.get(model_id)
    if supported is not None and effort != "inherit" and effort not in supported:
        raise ConfigError(f"{model_id} 不支持 reasoning_effort={effort}")


def validate_project_override(value: dict[str, Any]) -> None:
    reject_unknown_keys(value, PROJECT_KEYS, "项目配置")
    if value.get("schema_version") != 1:
        raise ConfigError("项目配置 schema_version 必须是 1")
    for section, allowed in NESTED_KEYS.items():
        if section not in value:
            continue
        nested = require_object(value[section], f"项目配置.{section}")
        reject_unknown_keys(nested, allowed, f"项目配置.{section}")
        if section == "models":
            for role, model in nested.items():
                validate_model(model, f"项目配置.models.{role}")


def validate_config(value: dict[str, Any], *, allow_pending: bool = True) -> None:
    reject_unknown_keys(value, FULL_KEYS, "配置")
    if set(value) != FULL_KEYS:
        missing = FULL_KEYS - set(value)
        raise ConfigError(f"配置缺少字段：{', '.join(sorted(missing))}")
    if value.get("schema_version") != 1:
        raise ConfigError("schema_version 必须是 1")

    onboarding = require_object(value["onboarding"], "onboarding")
    reject_unknown_keys(onboarding, {"first_run", "status", "completed_at"}, "onboarding")
    if set(onboarding) != {"first_run", "status", "completed_at"}:
        raise ConfigError("onboarding 必须包含 first_run/status/completed_at")
    if not isinstance(onboarding["first_run"], bool):
        raise ConfigError("onboarding.first_run 必须是布尔值")
    status = onboarding["status"]
    if status not in ONBOARDING_STATUSES:
        raise ConfigError("onboarding.status 必须是 pending/complete/deferred")
    completed_at = onboarding["completed_at"]
    if status == "pending":
        if not onboarding["first_run"] or completed_at is not None:
            raise ConfigError("pending 状态要求 first_run=true 且 completed_at=null")
        if not allow_pending:
            raise OnboardingRequired("首次配置尚未完成")
    else:
        if onboarding["first_run"] or not isinstance(completed_at, str) or not completed_at:
            raise ConfigError(f"{status} 状态要求 first_run=false 且 completed_at 非空")

    models = require_object(value["models"], "models")
    reject_unknown_keys(models, set(ROLES), "models")
    if set(models) != set(ROLES):
        raise ConfigError("models 必须包含 implementation/review/qa")
    for role in ROLES:
        validate_model(models[role], f"models.{role}")

    qa = require_object(value["qa"], "qa")
    reject_unknown_keys(qa, NESTED_KEYS["qa"], "qa")
    if set(qa) != NESTED_KEYS["qa"]:
        raise ConfigError("qa 必须包含 mode/driver/default_environment")
    if qa["mode"] not in QA_MODES:
        raise ConfigError("qa.mode 非法")
    driver = require_clean_optional_string(qa["driver"], "qa.driver")
    require_clean_optional_string(qa["default_environment"], "qa.default_environment")
    if status == "complete" and qa["mode"] == "ui_tasks" and not driver:
        raise ConfigError("自动 UI QA 已开启，但 qa.driver 尚未配置")
    if status == "deferred" and qa["mode"] != "disabled":
        raise ConfigError("deferred 状态必须使用 qa.mode=disabled")

    attempts = require_object(value["attempts"], "attempts")
    reject_unknown_keys(attempts, NESTED_KEYS["attempts"], "attempts")
    if set(attempts) != NESTED_KEYS["attempts"]:
        raise ConfigError("attempts 字段不完整")
    require_int(attempts["implementation_retries"], "attempts.implementation_retries", 0)
    require_int(attempts["review_max_attempts"], "attempts.review_max_attempts", 1)
    require_int(attempts["qa_max_attempts"], "attempts.qa_max_attempts", 1)

    watchdog = require_object(value["watchdog"], "watchdog")
    reject_unknown_keys(watchdog, NESTED_KEYS["watchdog"], "watchdog")
    if set(watchdog) != NESTED_KEYS["watchdog"]:
        raise ConfigError("watchdog 字段不完整")
    require_int(watchdog["poll_seconds"], "watchdog.poll_seconds", 1)
    require_int(watchdog["idle_timeout_seconds"], "watchdog.idle_timeout_seconds", 0)
    require_int(watchdog["max_runtime_seconds"], "watchdog.max_runtime_seconds", 0)
    require_int(watchdog["terminate_grace_seconds"], "watchdog.terminate_grace_seconds", 0)


def apply_environment_overrides(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    mappings = {
        "CODEX_IMPLEMENTATION_MODEL": ("models", "implementation", "id"),
        "CODEX_IMPLEMENTATION_REASONING_EFFORT": ("models", "implementation", "reasoning_effort"),
        "CODEX_REVIEW_MODEL": ("models", "review", "id"),
        "CODEX_REVIEW_REASONING_EFFORT": ("models", "review", "reasoning_effort"),
        "CODEX_QA_MODEL": ("models", "qa", "id"),
        "CODEX_QA_REASONING_EFFORT": ("models", "qa", "reasoning_effort"),
        "CODEX_QA_DRIVER": ("qa", "driver"),
        "CODEX_QA_ENVIRONMENT": ("qa", "default_environment"),
        "CODEX_POLL_SECONDS": ("watchdog", "poll_seconds"),
        "CODEX_IDLE_TIMEOUT_SECONDS": ("watchdog", "idle_timeout_seconds"),
        "CODEX_MAX_RUNTIME_SECONDS": ("watchdog", "max_runtime_seconds"),
        "CODEX_TERMINATE_GRACE_SECONDS": ("watchdog", "terminate_grace_seconds"),
    }
    for env_name, path in mappings.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        cursor: dict[str, Any] = result
        for part in path[:-1]:
            cursor = cursor[part]
        if path[0] == "watchdog":
            try:
                cursor[path[-1]] = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{env_name} 必须是整数，实际为：{raw!r}") from exc
        else:
            cursor[path[-1]] = raw
    return result


def user_config_path(explicit: str | None, *, for_write: bool = False) -> Path:
    value = explicit or os.environ.get("CODEX_LOOP_CONFIG")
    if value:
        return Path(value).expanduser().resolve()
    if not for_write and not DEFAULT_USER_PATH.exists() and LEGACY_USER_PATH.exists():
        return LEGACY_USER_PATH.resolve()
    return DEFAULT_USER_PATH.resolve()


def project_config_path(project: str | None) -> Path | None:
    explicit = os.environ.get("CODEX_LOOP_PROJECT_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if not project:
        return None
    project_path = Path(project).expanduser().resolve()
    current = project_path / PROJECT_RELATIVE_PATH
    legacy = project_path / LEGACY_PROJECT_RELATIVE_PATH
    return legacy if not current.exists() and legacy.exists() else current


def resolve_config(
    *,
    config_path: str | None = None,
    project: str | None = None,
    resolved_path: str | None = None,
    apply_env: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    frozen = resolved_path or os.environ.get("CODEX_LOOP_RESOLVED_CONFIG")
    if frozen:
        path = Path(frozen).expanduser().resolve()
        config = load_json(path, "配置快照")
        validate_config(config, allow_pending=False)
        return config, [str(path)]

    defaults = load_json(DEFAULTS_PATH, "内置默认配置")
    validate_config(defaults, allow_pending=True)
    path = user_config_path(config_path)
    if not path.is_file():
        raise OnboardingRequired(f"用户配置不存在：{path}")
    user = load_json(path, "用户配置")
    config = deep_merge(defaults, user)
    sources = [str(DEFAULTS_PATH), str(path)]
    project_path = project_config_path(project)
    if project_path and project_path.is_file():
        project_override = load_json(project_path, "项目配置")
        validate_project_override(project_override)
        config = deep_merge(config, project_override)
        sources.append(str(project_path))
    if apply_env:
        config = apply_environment_overrides(config)
    validate_config(config, allow_pending=False)
    return config, sources


def canonical_bytes(config: dict[str, Any]) -> bytes:
    return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def config_fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(config)).hexdigest()


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def onboarding_payload(path: Path, reason: str) -> dict[str, Any]:
    defaults = load_json(DEFAULTS_PATH, "内置默认配置")
    return {
        "result": "onboarding_required",
        "reason": reason,
        "config_path": str(path),
        "recommended": {
            "qa_mode": "ui_tasks",
            "models": defaults["models"],
            "attempts": defaults["attempts"],
            "watchdog": defaults["watchdog"],
        },
        "questions": [
            "是否只对 UI/用户流程任务自动开启 QA，还是每次询问或关闭？",
            "是否保持 implementation=5.6 Sol/xhigh、review=Astra/xhigh、QA=Astra/high？",
            "若开启自动 QA，使用哪个 driver？默认测试环境是否按项目配置？",
            "是否保持默认重试次数和 watchdog 超时？",
        ],
    }


def command_status(args: argparse.Namespace) -> None:
    path = user_config_path(args.config)
    try:
        config, sources = resolve_config(config_path=args.config, project=args.project)
    except OnboardingRequired as exc:
        print(json.dumps(onboarding_payload(path, str(exc)), ensure_ascii=False, indent=2))
        raise SystemExit(20) from exc
    print(
        json.dumps(
            {
                "result": "configured",
                "config_path": str(path),
                "sources": sources,
                "fingerprint": config_fingerprint(config),
                "onboarding": config["onboarding"],
                "models": config["models"],
                "qa": config["qa"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_configure(args: argparse.Namespace) -> None:
    defaults = load_json(DEFAULTS_PATH, "内置默认配置")
    config = copy.deepcopy(defaults)
    config["onboarding"] = {
        "first_run": False,
        "status": "complete",
        "completed_at": utc_now(),
    }
    for role in ROLES:
        config["models"][role] = {
            "id": getattr(args, f"{role}_model"),
            "reasoning_effort": getattr(args, f"{role}_effort"),
        }
    config["qa"].update(
        {
            "mode": args.qa_mode,
            "driver": args.qa_driver or None,
            "default_environment": args.qa_environment or None,
        }
    )
    config["attempts"].update(
        {
            "implementation_retries": args.implementation_retries,
            "review_max_attempts": args.review_max_attempts,
            "qa_max_attempts": args.qa_max_attempts,
        }
    )
    config["watchdog"].update(
        {
            "poll_seconds": args.poll_seconds,
            "idle_timeout_seconds": args.idle_timeout_seconds,
            "max_runtime_seconds": args.max_runtime_seconds,
            "terminate_grace_seconds": args.terminate_grace_seconds,
        }
    )
    validate_config(config, allow_pending=False)
    path = user_config_path(args.config, for_write=True)
    if path.exists() and not args.force:
        raise ConfigError(f"配置已存在；使用 --force 才能覆盖：{path}")
    atomic_write(path, config)
    print(path)


def command_defer(args: argparse.Namespace) -> None:
    config = load_json(DEFAULTS_PATH, "内置默认配置")
    config["onboarding"] = {
        "first_run": False,
        "status": "deferred",
        "completed_at": utc_now(),
    }
    config["qa"]["mode"] = "disabled"
    validate_config(config, allow_pending=False)
    path = user_config_path(args.config, for_write=True)
    if path.exists() and not args.force:
        raise ConfigError(f"配置已存在；使用 --force 才能覆盖：{path}")
    atomic_write(path, config)
    print(path)


def command_reset(args: argparse.Namespace) -> None:
    source_path = user_config_path(args.config)
    path = user_config_path(args.config, for_write=True)
    defaults = load_json(DEFAULTS_PATH, "默认配置")
    user = load_json(source_path, "用户配置") if source_path.is_file() else {}
    config = deep_merge(defaults, user)
    config["onboarding"] = {"first_run": True, "status": "pending", "completed_at": None}
    validate_config(config, allow_pending=True)
    atomic_write(path, config)
    print(path)


def command_validate(args: argparse.Namespace) -> None:
    path = Path(args.path).expanduser().resolve()
    config = load_json(path, "配置")
    validate_config(config, allow_pending=args.allow_pending)
    print(json.dumps({"result": "valid", "fingerprint": config_fingerprint(config)}, ensure_ascii=False))


def command_resolve(args: argparse.Namespace) -> None:
    config, _ = resolve_config(
        config_path=args.config,
        project=args.project,
        resolved_path=args.resolved_config,
    )
    if args.output:
        output = Path(args.output).expanduser().resolve()
        atomic_write(output, config)
        print(output)
    else:
        print(json.dumps(config, ensure_ascii=False, indent=2))


def command_runtime(args: argparse.Namespace) -> None:
    config, _ = resolve_config(
        config_path=args.config,
        project=args.project,
        resolved_path=args.resolved_config,
    )
    model = config["models"][args.role]
    values = [
        model["id"],
        model["reasoning_effort"],
        config["qa"]["driver"] or "",
        config["qa"]["default_environment"] or "",
        config["qa"]["mode"],
        str(config["watchdog"]["poll_seconds"]),
        str(config["watchdog"]["idle_timeout_seconds"]),
        str(config["watchdog"]["max_runtime_seconds"]),
        str(config["watchdog"]["terminate_grace_seconds"]),
        config_fingerprint(config),
    ]
    print("\x1e".join(values))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="检查首次配置和有效配置")
    status.add_argument("--config")
    status.add_argument("--project")
    status.set_defaults(func=command_status)

    configure = subparsers.add_parser("configure", help="写入已完成的用户配置")
    configure.add_argument("--config")
    configure.add_argument("--force", action="store_true")
    configure.add_argument("--qa-mode", choices=sorted(QA_MODES), default="ui_tasks")
    configure.add_argument("--qa-driver", default="")
    configure.add_argument("--qa-environment", default="")
    configure.add_argument("--implementation-model", default="gpt-5.6-sol")
    configure.add_argument("--implementation-effort", choices=sorted(EFFORTS), default="xhigh")
    configure.add_argument("--review-model", default="gpt-6-astra")
    configure.add_argument("--review-effort", choices=sorted(EFFORTS), default="xhigh")
    configure.add_argument("--qa-model", default="gpt-6-astra")
    configure.add_argument("--qa-effort", choices=sorted(EFFORTS), default="high")
    configure.add_argument("--implementation-retries", type=int, default=2)
    configure.add_argument("--review-max-attempts", type=int, default=5)
    configure.add_argument("--qa-max-attempts", type=int, default=5)
    configure.add_argument("--poll-seconds", type=int, default=20)
    configure.add_argument("--idle-timeout-seconds", type=int, default=1200)
    configure.add_argument("--max-runtime-seconds", type=int, default=7200)
    configure.add_argument("--terminate-grace-seconds", type=int, default=10)
    configure.set_defaults(func=command_configure)

    defer = subparsers.add_parser("defer", help="暂缓向导并安全关闭自动 QA")
    defer.add_argument("--config")
    defer.add_argument("--force", action="store_true")
    defer.set_defaults(func=command_defer)

    reset = subparsers.add_parser("reset-onboarding", help="保留偏好并重新触发首次向导")
    reset.add_argument("--config")
    reset.set_defaults(func=command_reset)

    validate = subparsers.add_parser("validate", help="校验一个完整配置或快照")
    validate.add_argument("path")
    validate.add_argument("--allow-pending", action="store_true")
    validate.set_defaults(func=command_validate)

    resolve = subparsers.add_parser("resolve", help="合并配置层并生成运行快照")
    resolve.add_argument("--config")
    resolve.add_argument("--project")
    resolve.add_argument("--resolved-config")
    resolve.add_argument("--output")
    resolve.set_defaults(func=command_resolve)

    runtime = subparsers.add_parser("runtime", help="输出 shell 封装所需的角色配置")
    runtime.add_argument("--role", choices=ROLES, required=True)
    runtime.add_argument("--config")
    runtime.add_argument("--project")
    runtime.add_argument("--resolved-config")
    runtime.set_defaults(func=command_runtime)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except OnboardingRequired as exc:
        path = user_config_path(getattr(args, "config", None))
        print(json.dumps(onboarding_payload(path, str(exc)), ensure_ascii=False, indent=2))
        raise SystemExit(20) from exc
    except ConfigError as exc:
        raise SystemExit(f"配置错误：{exc}") from exc


if __name__ == "__main__":
    main()
