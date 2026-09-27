"""Behavioral tests using real CLI stdin and isolated state/settings directories."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude-loop-hook.py"


class ClaudeLoopHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude loop tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bindings = self.root / "bindings"
        self.state_path = self.root / "state.json"
        self.packet = self.root / "T001.md"
        self.packet.write_text("Implement one bounded change.\n")
        self.state = {
            "schema_version": 1, "status": "active", "deadline": "",
            "worktree": str(self.root), "max_retries_per_task": 2,
            "config_snapshot": {}, "checkpoint_mode": "commit", "history": [],
            "tasks": [{
                "id": "T001", "title": "one change", "packet": str(self.packet),
                "depends_on": [], "status": "pending", "attempts": 0,
                "sessions": [], "runs": [], "reviews": [], "qa_runs": [],
            }],
        }
        self.save_state()
        self.env = os.environ.copy()
        for key in ("CODEX_LOOP_CC_SESSION_ID", "CODEX_LOOP_BINDINGS_DIR", "CLAUDE_ENV_FILE"):
            self.env.pop(key, None)

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def cli(self, command, *args, payload=None, env=None, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--bindings-dir", str(self.bindings), *map(str, args)],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, env=env or self.env, timeout=15,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def arm(self, sid="session-a", *args):
        return self.cli("arm", self.state_path, "--session-id", sid, *args)

    def hook(self, kind="Stop", sid="session-a", **fields):
        return self.cli("hook", payload={"hook_event_name": kind, "session_id": sid, "cwd": str(self.root), **fields})

    def status(self, sid="session-a"):
        return json.loads(self.cli("status", "--session-id", sid).stdout)

    def test_install_preserves_settings_and_existing_hooks_and_is_idempotent(self):
        settings = self.root / "settings.json"
        existing = {"permissions": {"allow": ["Read"]}, "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo previous"}]}],
            "PostToolUse": [{"matcher": "Write", "hooks": []}],
        }}
        original = json.dumps(existing, indent=4)
        settings.write_text(original)
        result = json.loads(self.cli("install", "--settings", settings).stdout)
        self.assertEqual(Path(result["backup"]).read_text(), original)
        after = json.loads(settings.read_text())
        self.assertEqual(after["permissions"], existing["permissions"])
        self.assertEqual(after["hooks"]["PostToolUse"], existing["hooks"]["PostToolUse"])
        self.assertEqual(after["hooks"]["Stop"][0], existing["hooks"]["Stop"][0])
        self.assertEqual(len(after["hooks"]["Stop"]), 2)
        second = json.loads(self.cli("install", "--settings", settings).stdout)
        self.assertFalse(second["changed"])
        self.assertEqual(json.loads(settings.read_text()), after)
        self.assertFalse(self.bindings.exists(), "install must not arm any session")

    def test_disabled_or_malformed_settings_are_not_replaced(self):
        settings = self.root / "settings.json"
        for original in ('{"disableAllHooks": true}', '{"hooks": []}', '{not json'):
            with self.subTest(original=original):
                settings.write_text(original)
                self.cli("install", "--settings", settings, success=False)
                self.assertEqual(settings.read_text(), original)

    def test_arm_requires_current_identity_and_does_not_guess(self):
        result = self.cli("arm", self.state_path, success=False)
        self.assertIn("session ID", result.stderr)
        self.assertFalse(self.bindings.exists())

    def test_session_start_exports_exact_identity_without_arming(self):
        env_file = self.root / "session env.sh"
        env_file.write_text("export EXISTING=yes\n")
        env = {**self.env, "CLAUDE_ENV_FILE": str(env_file)}
        sid = "current session's ID"
        result = self.cli("hook", env=env, payload={"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"})
        self.assertEqual(result.stdout, "")
        exported = subprocess.run(
            ["sh", "-c", '. "$1"; printf "%s\\n%s\\n%s" "$EXISTING" "$CODEX_LOOP_CC_SESSION_ID" "$CODEX_LOOP_BINDINGS_DIR"', "test", str(env_file)],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(exported.stdout.splitlines(), ["yes", sid, str(self.bindings)])
        self.assertFalse(self.bindings.exists())
        self.cli("arm", self.state_path, env={**self.env, "CODEX_LOOP_CC_SESSION_ID": sid})
        self.assertTrue(self.status(sid)["armed"])

    def test_only_explicitly_bound_main_session_is_continued(self):
        self.arm()
        self.assertEqual(self.hook(sid="unrelated-session").stdout, "")
        self.assertEqual(self.hook(agent_id="child-agent").stdout, "")
        self.assertEqual(self.hook("SubagentStop").stdout, "")
        result = json.loads(self.hook(stop_hook_active=False).stdout)
        self.assertEqual(result["decision"], "block")
        self.assertIn(str(self.packet), result["reason"])
        self.assertEqual(self.status()["stalled_stops"], 1)

    def test_compact_and_resume_inject_context_but_not_other_session(self):
        (self.root / "run-contract.md").write_text("Scope, authorization and acceptance commands.")
        self.arm()
        for source in ("compact", "resume"):
            result = json.loads(self.hook("SessionStart", source=source).stdout)
            context = result["hookSpecificOutput"]["additionalContext"]
            for expected in (str(self.state_path), "config_snapshot", str(self.root), "next=ready", "checkpoint_mode=commit", str(self.root / "run-contract.md")):
                self.assertIn(expected, context)
        self.assertEqual(self.hook("SessionStart", sid="new-session", source="resume").stdout, "")
        self.assertEqual(self.status()["stalled_stops"], 0)

    def test_no_progress_is_bounded_even_when_notes_or_timestamps_change(self):
        self.arm()
        for index in range(3):
            self.state["updated_at"] = str(index)
            self.state["tasks"][0]["last_note"] = str(index)
            self.state["history"].append({"note": str(index)})
            self.save_state()
            self.assertEqual(json.loads(self.hook(stop_hook_active=True).stdout)["decision"], "block")
        result = json.loads(self.hook(stop_hook_active=True).stdout)
        self.assertNotIn("decision", result)
        self.assertFalse(self.status()["armed"])
        self.assertEqual(self.status()["disarm_reason"], "no-progress-stop-limit")
        self.assertEqual(self.hook().stdout, "")

    def test_meaningful_state_progress_resets_stall_counter(self):
        self.arm("session-a", "--max-stalled-stops", "1")
        self.hook()
        task = self.state["tasks"][0]
        task.update(status="running", attempts=1, sessions=[{"id": "codex-new"}])
        self.save_state()
        result = json.loads(self.hook(stop_hook_active=True).stdout)
        self.assertEqual(result["decision"], "block")
        self.assertIn("next=recover", result["reason"])
        self.assertEqual(self.status()["stalled_stops"], 1)

    def test_worker_log_growth_is_observed_without_state_transition(self):
        output = self.root / "worker"
        output.mkdir()
        events = output / "events.jsonl"
        events.write_text("event 1\n")
        self.state["tasks"][0].update(status="running", attempts=1, runs=[{"output_dir": str(output)}])
        self.save_state()
        self.arm("session-a", "--max-stalled-stops", "1")
        self.hook()
        events.write_text("event 1\nevent 2\n")
        self.assertEqual(json.loads(self.hook(stop_hook_active=True).stdout)["decision"], "block")
        self.assertTrue(self.status()["armed"])

    def test_quiet_live_worker_uses_watchdog_budget_not_stop_frequency(self):
        output = self.root / "worker"
        output.mkdir()
        events = output / "events.jsonl"
        events.write_text("")
        watchdog = output / "watchdog.json"
        watchdog.write_text(json.dumps({"result": "running", "pid": os.getpid(), "runtime_seconds": 0}))
        self.state["tasks"][0].update(status="running", attempts=1, runs=[{"output_dir": str(output)}])
        self.state["config_snapshot"]["watchdog"] = {"idle_timeout_seconds": 60, "max_runtime_seconds": 120}
        self.save_state()
        self.arm("session-a", "--max-stalled-stops", "1")
        for _ in range(3):
            self.assertEqual(json.loads(self.hook(stop_hook_active=True).stdout)["decision"], "block")
        self.assertEqual(self.status()["stalled_stops"], 0)
        old = time.time() - 180
        os.utime(events, (old, old))
        os.utime(watchdog, (old, old))
        self.hook(stop_hook_active=True)
        result = json.loads(self.hook(stop_hook_active=True).stdout)
        self.assertNotIn("decision", result)
        self.assertFalse(self.status()["armed"])

    def test_exhausted_task_is_parked_before_continuing(self):
        self.state["tasks"][0].update(status="retrying", attempts=3)
        self.save_state()
        self.arm()
        result = json.loads(self.hook().stdout)
        self.assertEqual(result["decision"], "block")
        self.assertIn("next=park", result["reason"])

    def test_parked_task_waiting_for_replan_survives_stop_and_compact(self):
        worktree = self.root / "repo"
        worktree.mkdir()
        artifact = self.root / "parked.md"
        artifact.write_text("Original task and partial work preserved before splitting.\n")
        self.state["worktree"] = str(worktree)
        self.state["tasks"][0].update(status="blocked", parked=True, replan_pending=True,
                                      blocked_artifact=str(artifact))
        self.state.update(max_replans=2, replan_count=0)
        self.save_state()
        self.arm()
        result = json.loads(self.hook().stdout)
        self.assertEqual(result["decision"], "block")
        self.assertIn("next=replan", result["reason"])
        resumed = json.loads(self.hook("SessionStart", source="compact").stdout)
        self.assertIn("next=replan", resumed["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.status()["armed"])

    def test_unparked_replan_keeps_intent_in_stop_instructions(self):
        self.state["tasks"][0].update(status="blocked", parked=False, replan_pending=True)
        self.save_state()
        self.arm()
        result = json.loads(self.hook().stdout)
        self.assertIn("next=park", result["reason"])
        self.assertIn("--replan", result["reason"])
        self.assertTrue(self.status()["armed"])

    def test_exhausted_replan_tells_controller_to_abandon_intent(self):
        artifact = self.root / "parked.md"
        artifact.write_text("Preserved task evidence.\n")
        self.state["tasks"][0].update(status="blocked", parked=True, replan_pending=True,
                                      blocked_artifact=str(artifact))
        self.state.update(max_replans=2, replan_count=2)
        self.save_state()
        self.arm()
        result = json.loads(self.hook().stdout)
        self.assertIn("next=park", result["reason"])
        self.assertIn("显式放弃重拆意图", result["reason"])
        self.assertTrue(self.status()["armed"])

    def test_terminal_conditions_disarm_and_do_not_restart_on_resume(self):
        cases = (
            ("deadline", lambda: self.state.update(deadline="2000-01-01T00:00:00Z")),
            ("paused", lambda: self.state.update(status="paused")),
            ("blocked", lambda: self.state["tasks"][0].update(status="blocked", parked=True)),
        )
        for reason, change in cases:
            with self.subTest(reason=reason):
                self.state.update(status="active", deadline="")
                self.state["tasks"][0].update(status="pending")
                self.save_state()
                self.arm()
                change()
                self.save_state()
                self.assertNotIn("decision", json.loads(self.hook().stdout))
                self.assertEqual(self.status()["disarm_reason"], reason)
                self.assertEqual(self.hook("SessionStart", source="resume").stdout, "")

    def test_completion_requires_finalization_then_explicit_disarm(self):
        self.state["tasks"][0]["status"] = "done"
        self.save_state()
        self.arm()
        result = json.loads(self.hook().stdout)
        self.assertEqual(result["decision"], "block")
        self.assertIn("completed", result["reason"])
        self.assertTrue(self.status()["armed"])
        self.cli("disarm", "--session-id", "session-a", "--reason", "completed")
        self.assertEqual(self.hook().stdout, "")

    def test_explicit_pause_survives_resume_and_handoff_is_explicit(self):
        self.arm()
        self.cli("arm", self.state_path, "--session-id", "session-b", success=False)
        self.cli("arm", self.state_path, "--session-id", "session-a", success=False)
        self.cli("disarm", "--session-id", "session-a", "--reason", "user-pause")
        self.assertEqual(self.hook("SessionStart", source="compact").stdout, "")
        self.assertEqual(self.hook().stdout, "")
        self.arm("session-b")
        self.assertEqual(json.loads(self.hook(sid="session-b").stdout)["decision"], "block")
        self.assertFalse(self.status("session-a")["armed"])

    def test_corrupt_bound_state_fails_open_with_diagnostic(self):
        self.arm()
        self.state_path.write_text("not json")
        result = self.cli("hook", payload={"session_id": "session-a", "hook_event_name": "Stop"}, success=False)
        self.assertEqual(result.stdout, "")
        self.assertIn("codex-loop:", result.stderr)


if __name__ == "__main__":
    unittest.main()
