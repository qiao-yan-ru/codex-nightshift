#!/usr/bin/env python3
"""验证 Codex QA 与 Claude Code 复测证据，只使用 Python 标准库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"{label} 不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} 不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} 顶层必须是对象")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_evidence(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValidationError(f"证据路径必须是非空相对路径：{relative!r}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError(f"证据路径越出目录：{relative}") from exc
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValidationError(f"证据文件不存在或为空：{resolved}")
    return resolved


def validate_screenshot(root: Path, item: Any) -> tuple[str, str]:
    if not isinstance(item, dict):
        raise ValidationError("截图记录必须是对象")
    for key in ("path", "sha256", "checkpoint"):
        if not isinstance(item.get(key), str) or not item[key]:
            raise ValidationError(f"截图记录缺少 {key}")
    path = resolve_evidence(root, item["path"])
    actual = sha256_file(path)
    if item["sha256"].lower() != actual:
        raise ValidationError(f"截图哈希不匹配：{item['path']}")
    return item["path"], actual


def evidence_fingerprint(records: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for path, file_hash in sorted(records):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} 必须是数组")
    return value


def validate_run(
    result_path: Path,
    evidence_dir: Path,
    expected_model: str,
    expected_reasoning_effort: str,
    expected_driver: str,
    expected_environment: str,
    expected_fingerprint: str,
) -> dict[str, Any]:
    result = load_json(result_path, "QA 结果")
    expected = {
        "model": expected_model,
        "reasoning_effort": expected_reasoning_effort,
        "driver": expected_driver,
        "environment": expected_environment,
        "target_fingerprint": expected_fingerprint,
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise ValidationError(f"QA 结果 {key} 不匹配：期望 {value!r}")

    verdict = result.get("verdict")
    if verdict not in {"pass", "fail", "blocked"}:
        raise ValidationError("QA verdict 必须是 pass/fail/blocked")
    cases = require_list(result.get("cases"), "cases")
    gaps = require_list(result.get("coverage_gaps"), "coverage_gaps")
    require_list(result.get("residual_risks"), "residual_risks")
    if not isinstance(result.get("summary"), str):
        raise ValidationError("summary 必须是字符串")

    seen_ids: set[str] = set()
    counts = {"pass": 0, "fail": 0, "blocked": 0}
    screenshots: list[tuple[str, str]] = []
    screenshot_cases: dict[str, str] = {}
    required_retest_case_ids: set[str] = set()
    failure_rows: list[list[Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValidationError("QA case 必须是对象")
        case_id = case.get("id")
        case_result = case.get("result")
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise ValidationError(f"QA case id 为空或重复：{case_id!r}")
        seen_ids.add(case_id)
        if case_result not in counts:
            raise ValidationError(f"QA case {case_id} result 非法")
        priority = case.get("priority")
        if priority not in {"critical", "high", "normal"}:
            raise ValidationError(f"QA case {case_id} priority 非法")
        counts[case_result] += 1
        if priority in {"critical", "high"} or case_result == "fail":
            required_retest_case_ids.add(case_id)
        assertions = require_list(case.get("assertions"), f"{case_id}.assertions")
        case_shots = require_list(case.get("screenshots"), f"{case_id}.screenshots")
        require_list(case.get("steps_executed"), f"{case_id}.steps_executed")
        console_errors = require_list(case.get("console_errors"), f"{case_id}.console_errors")
        network_errors = require_list(case.get("network_errors"), f"{case_id}.network_errors")
        require_list(case.get("notes"), f"{case_id}.notes")
        if case_result in {"pass", "fail"} and (not assertions or not case_shots):
            raise ValidationError(f"QA case {case_id} 缺少断言或截图")
        paths: list[str] = []
        for screenshot in case_shots:
            record = validate_screenshot(evidence_dir, screenshot)
            screenshots.append(record)
            paths.append(record[0])
            screenshot_cases[record[0]] = case_id
        if case_result == "fail":
            assertion_failed = any(
                isinstance(item, dict) and item.get("result") == "fail" for item in assertions
            )
            if not assertion_failed and not console_errors and not network_errors:
                raise ValidationError(f"失败用例 {case_id} 没有失败断言或错误证据")
            failure_rows.append([case_id, str(case.get("title", "")), paths])

    if len({path for path, _ in screenshots}) != len(screenshots):
        raise ValidationError("同一截图路径被多个记录重复使用")
    if verdict == "pass" and (not cases or counts["fail"] or counts["blocked"] or gaps):
        raise ValidationError("verdict=pass 要求至少一个用例、全部通过且无 coverage gap")
    if verdict == "fail" and counts["fail"] == 0:
        raise ValidationError("verdict=fail 至少需要一个失败用例")
    if verdict == "blocked" and counts["blocked"] == 0 and not gaps:
        raise ValidationError("verdict=blocked 必须有 blocked 用例或 coverage gap")

    result_hash = sha256_file(result_path)
    evidence_hash = evidence_fingerprint(screenshots)
    return {
        "kind": "qa-run",
        "verdict": verdict,
        "model": expected_model,
        "reasoning_effort": expected_reasoning_effort,
        "driver": expected_driver,
        "environment": expected_environment,
        "result_sha256": result_hash,
        "evidence_fingerprint": evidence_hash,
        "screenshot_count": len(screenshots),
        "screenshot_paths": sorted(path for path, _ in screenshots),
        "screenshot_cases": screenshot_cases,
        "required_retest_case_ids": sorted(required_retest_case_ids),
        "case_counts": counts,
        "failure_rows": failure_rows,
        "coverage_gaps": gaps,
        "target_fingerprint": expected_fingerprint,
    }


def validate_audit(
    audit_path: Path,
    audit_evidence_dir: Path,
    qa_validation_path: Path,
) -> dict[str, Any]:
    audit = load_json(audit_path, "CC QA 审计")
    qa_validation = load_json(qa_validation_path, "QA validation")
    decision = audit.get("decision")
    if decision not in {"accept", "retry", "blocked"}:
        raise ValidationError("CC QA decision 必须是 accept/retry/blocked")
    if audit.get("qa_evidence_fingerprint") != qa_validation.get("evidence_fingerprint"):
        raise ValidationError("CC QA 审计引用的 evidence fingerprint 不匹配")
    screenshot_reviews = require_list(audit.get("screenshot_reviews"), "screenshot_reviews")
    retested = require_list(audit.get("retested_cases"), "retested_cases")
    findings = require_list(audit.get("findings"), "findings")
    gaps = require_list(audit.get("coverage_gaps"), "coverage_gaps")
    require_list(audit.get("residual_risks"), "residual_risks")
    if not isinstance(audit.get("summary"), str):
        raise ValidationError("CC QA summary 必须是字符串")

    reviewed_paths: set[str] = set()
    qa_screenshot_paths = set(qa_validation.get("screenshot_paths", []))
    screenshot_cases = qa_validation.get("screenshot_cases", {})
    if not isinstance(screenshot_cases, dict):
        raise ValidationError("QA validation 缺少 screenshot_cases")
    for item in screenshot_reviews:
        if not isinstance(item, dict) or item.get("result") not in {"pass", "fail"}:
            raise ValidationError("screenshot review 记录非法")
        relative = item.get("path")
        if not isinstance(relative, str):
            raise ValidationError("screenshot review 缺少 path")
        if relative not in qa_screenshot_paths:
            raise ValidationError(f"CC 声称审查了不属于本次 QA 的截图：{relative}")
        if item.get("case_id") != screenshot_cases.get(relative):
            raise ValidationError(f"CC 截图审查的 case_id 与原 QA 证据不匹配：{relative}")
        if relative in reviewed_paths:
            raise ValidationError(f"CC 重复登记截图审查：{relative}")
        reviewed_paths.add(relative)

    audit_evidence: list[tuple[str, str]] = []
    retest_results: list[str] = []
    retested_ids: set[str] = set()
    for item in retested:
        if not isinstance(item, dict) or item.get("result") not in {"pass", "fail", "blocked"}:
            raise ValidationError("retested case 记录非法")
        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in retested_ids:
            raise ValidationError(f"retested case id 为空或重复：{case_id!r}")
        retested_ids.add(case_id)
        retest_results.append(item["result"])
        evidence = require_list(item.get("evidence"), "retested case evidence")
        if item["result"] in {"pass", "fail"} and not evidence:
            raise ValidationError(f"复测用例 {item.get('id')} 缺少独立截图证据")
        for screenshot in evidence:
            audit_evidence.append(validate_screenshot(audit_evidence_dir, screenshot))

    required_retests = set(qa_validation.get("required_retest_case_ids", []))
    missing_retests = required_retests - retested_ids
    if missing_retests:
        raise ValidationError(
            "CC 未独立复测 Codex 的关键/高风险/失败用例：" + ", ".join(sorted(missing_retests))
        )
    required_review_paths = {
        path for path, case_id in screenshot_cases.items() if case_id in required_retests
    }
    missing_reviews = required_review_paths - reviewed_paths
    if missing_reviews:
        raise ValidationError(
            "CC 未审查关键/高风险/失败用例的全部截图：" + ", ".join(sorted(missing_reviews))
        )

    if decision == "accept":
        if not screenshot_reviews or not retested:
            raise ValidationError("accept 要求 CC 审查截图并至少独立复测一个用例")
        if findings or gaps or any(result != "pass" for result in retest_results):
            raise ValidationError("accept 不允许 findings、coverage gap 或未通过复测")
    elif decision == "retry":
        if not retested or (not findings and "fail" not in retest_results):
            raise ValidationError("retry 必须有独立复测，并包含 finding 或失败复测")
    elif decision == "blocked" and not gaps and "blocked" not in retest_results:
        raise ValidationError("blocked 必须有 coverage gap 或 blocked 复测")

    return {
        "kind": "qa-audit",
        "decision": decision,
        "audit_sha256": sha256_file(audit_path),
        "audit_evidence_fingerprint": evidence_fingerprint(audit_evidence),
        "reviewed_screenshot_count": len(reviewed_paths),
        "retested_case_count": len(retested),
        "finding_count": len(findings),
        "coverage_gap_count": len(gaps),
    }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_summary(validation: dict[str, Any]) -> str:
    counts = validation["case_counts"]
    lines = [
        "# Codex QA 摘要",
        "",
        f"- verdict: `{validation['verdict']}`",
        f"- model/reasoning: `{validation['model']}/{validation['reasoning_effort']}`",
        f"- cases: pass={counts['pass']}, fail={counts['fail']}, blocked={counts['blocked']}",
        f"- screenshots: {validation['screenshot_count']}",
        f"- evidence fingerprint: `{validation['evidence_fingerprint']}`",
    ]
    if validation["failure_rows"]:
        lines.extend(["", "## 失败用例", ""])
        for case_id, title, paths in validation["failure_rows"]:
            lines.append(f"- `{case_id}` {title}: {', '.join(paths)}")
    if validation["coverage_gaps"]:
        lines.extend(["", "## Coverage gaps", ""])
        lines.extend(f"- {item}" for item in validation["coverage_gaps"])
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("validate-run")
    run.add_argument("result")
    run.add_argument("evidence_dir")
    run.add_argument("--expected-model", required=True)
    run.add_argument("--expected-reasoning-effort", required=True)
    run.add_argument("--expected-driver", required=True)
    run.add_argument("--expected-environment", required=True)
    run.add_argument("--expected-fingerprint", required=True)
    run.add_argument("--validation", required=True)
    run.add_argument("--summary", required=True)
    audit = subparsers.add_parser("validate-audit")
    audit.add_argument("audit")
    audit.add_argument("audit_evidence_dir")
    audit.add_argument("qa_validation")
    audit.add_argument("--validation", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate-run":
            validation = validate_run(
                Path(args.result).resolve(),
                Path(args.evidence_dir).resolve(),
                args.expected_model,
                args.expected_reasoning_effort,
                args.expected_driver,
                args.expected_environment,
                args.expected_fingerprint,
            )
            atomic_write(Path(args.validation).resolve(), json.dumps(validation, ensure_ascii=False, indent=2) + "\n")
            atomic_write(Path(args.summary).resolve(), run_summary(validation))
        else:
            validation = validate_audit(
                Path(args.audit).resolve(),
                Path(args.audit_evidence_dir).resolve(),
                Path(args.qa_validation).resolve(),
            )
            atomic_write(Path(args.validation).resolve(), json.dumps(validation, ensure_ascii=False, indent=2) + "\n")
    except ValidationError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
