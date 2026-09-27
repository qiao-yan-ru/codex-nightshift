"""Exercise the persisted CLI contract without invoking Codex or Claude Code."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-state.py"


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state.json"
        self.git("init", "-q")
        self.git("config", "user.name", "State tests")
        self.git("config", "user.email", "state@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.initial_head = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                text=True, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def output(self, *args):
        return json.loads(self.cli(*args).stdout)

    def init(self, *extra):
        self.cli("init", self.state, "--objective", "finish objective", "--worktree", self.repo,
                 "--branch", "test", "--base-branch", "main", *extra)

    def packet(self, task_id):
        path = self.root / (task_id + ".md")
        path.write_text("Implement and verify " + task_id)
        return path

    def add(self, task_id, *extra):
        return self.output("add", self.state, "--id", task_id, "--title", task_id,
                           "--packet", self.packet(task_id), *extra)

    def read(self):
        return json.loads(self.state.read_text())

    def alter_task(self, task_id, **changes):
        state = self.read()
        next(task for task in state["tasks"] if task["id"] == task_id).update(changes)
        self.state.write_text(json.dumps(state))

    def start(self, task_id, *extra, success=True):
        return self.cli("start", self.state, task_id, "--output-dir", self.root / (task_id + "-run"),
                        *extra, success=success)

    def artifact(self, name="parked.json"):
        path = self.root / name
        path.write_text(json.dumps({"changes": "none", "processes_stopped": True}))
        return path

    def park(self, task_id):
        return self.cli("set", self.state, task_id, "blocked", "--note", "task too large",
                        "--artifact", self.artifact(task_id + "-parked.json"))

    def plan(self, parent, *ids, dependencies=None):
        plan = self.root / (parent + "-plan.json")
        tasks = []
        for task_id in ids:
            packet = self.packet(task_id)
            tasks.append({"id": task_id, "title": task_id, "packet": packet.name,
                          "depends_on": (dependencies or {}).get(task_id, [])})
        plan.write_text(json.dumps({"tasks": tasks}))
        return plan

    def split(self, parent, plan, success=True):
        return self.cli("split", self.state, parent, "--plan", plan,
                        "--note", "Separate independently verifiable work", success=success)

    def test_exhausted_task_is_parked_then_independent_task_runs(self):
        self.init("--max-retries", "0")
        self.add("A")
        self.add("B")
        self.start("A")
        self.cli("set", self.state, "A", "retrying", "--note", "context exhausted")
        self.assertEqual(self.output("next", self.state)["result"], "park")
        self.start("B", success=False)
        self.park("A")
        self.assertEqual(self.output("next", self.state)["task"]["id"], "B")
        self.start("B")

    def test_unparked_blocked_task_prevents_silent_continuation(self):
        self.init()
        self.add("A")
        self.add("B")
        self.start("A")
        self.cli("set", self.state, "A", "blocked", "--note", "stalled")
        self.assertEqual(self.output("next", self.state)["reason"], "blocked_not_parked")
        self.park("A")
        self.assertEqual(self.output("next", self.state)["result"], "ready")

    def test_dependency_blocked_summary_matches_scheduler(self):
        self.init()
        self.add("A")
        self.add("B", "--depends-on", "A")
        self.cli("set", self.state, "A", "blocked", "--note", "missing prerequisite")
        self.assertEqual(self.output("next", self.state)["result"], "blocked")
        self.assertEqual(self.output("summary", self.state)["status"], "blocked")
        self.start("B", success=False)

    def test_review_budget_allows_current_attempt_to_finish_then_parks(self):
        self.init("--max-review-attempts", "1")
        self.add("A")
        self.alter_task("A", status="reviewing", review_attempts=1,
                        reviews=[{"status": "running", "decision": ""}])
        self.assertEqual(self.output("next", self.state)["result"], "recover")
        self.alter_task("A", reviews=[{"status": "error", "decision": "error"}])
        self.assertEqual(self.output("next", self.state)["reason"], "review_budget_exhausted")

    def test_qa_budget_is_task_local(self):
        self.init("--max-qa-attempts", "1")
        self.add("A", "--qa-required")
        self.alter_task("A", status="verifying", qa_attempts=1)
        self.assertEqual(self.output("next", self.state)["reason"], "qa_budget_exhausted")

    def test_parking_never_discards_dirty_changes(self):
        self.init()
        self.add("A")
        self.start("A")
        (self.repo / "tracked.txt").write_text("unfinished change\n")
        before = self.state.read_bytes()
        self.cli("set", self.state, "A", "blocked", "--note", "park",
                 "--artifact", self.artifact(), success=False)
        self.assertEqual(before, self.state.read_bytes())
        self.assertEqual((self.repo / "tracked.txt").read_text(), "unfinished change\n")

    def test_parking_rejects_empty_or_inside_worktree_artifacts(self):
        self.init()
        self.add("A")
        empty = self.root / "empty.patch"
        empty.touch()
        self.cli("set", self.state, "A", "blocked", "--note", "park", "--artifact", empty, success=False)
        self.cli("set", self.state, "A", "blocked", "--note", "park", "--artifact",
                 self.repo / "tracked.txt", success=False)

    def test_split_preserves_dependencies_qa_history_and_fresh_sessions(self):
        self.init()
        self.add("P")
        self.alter_task("P", status="done")
        self.add("A", "--depends-on", "P", "--qa-required", "--task-kind", "ui")
        self.add("D", "--depends-on", "A")
        self.start("A")
        self.cli("session", self.state, "A", "old-session")
        self.park("A")
        self.split("A", self.plan("A", "A1", "A2", dependencies={"A2": ["A1"]}))
        tasks = {task["id"]: task for task in self.read()["tasks"]}
        self.assertEqual(tasks["A"]["status"], "superseded")
        self.assertEqual(tasks["A"]["sessions"][0]["id"], "old-session")
        self.assertEqual(tasks["D"]["depends_on"], ["A1", "A2"])
        self.assertEqual(tasks["A2"]["depends_on"], ["P", "A1"])
        self.assertTrue(tasks["A1"]["qa_required"])
        self.assertEqual(tasks["A1"]["task_kind"], "ui")
        self.assertEqual(tasks["A1"]["sessions"], [])
        self.assertEqual(tasks["A1"]["attempts"], 0)
        self.assertEqual(self.output("next", self.state)["task"]["id"], "A1")
        self.start("D", success=False)
        self.start("A1", "--resume-session", "old-session", success=False)
        self.start("A1")
        self.cli("session", self.state, "A1", "old-session", success=False)
        self.cli("session", self.state, "A1", "child-session")

    def test_completion_ignores_superseded_parent_but_requires_all_children(self):
        self.init()
        self.add("A")
        self.park("A")
        self.split("A", self.plan("A", "A1", "A2"))
        self.alter_task("A1", status="done")
        self.assertEqual(self.output("next", self.state)["task"]["id"], "A2")
        self.alter_task("A2", status="done")
        self.assertEqual(self.output("next", self.state)["result"], "complete")
        self.assertEqual(self.output("summary", self.state)["status"], "completed")

    def test_split_cycle_rejected_atomically(self):
        self.init()
        self.add("A")
        self.add("D", "--depends-on", "A")
        self.park("A")
        before = self.state.read_bytes()
        self.split("A", self.plan("A", "A1", "A2", dependencies={"A1": ["D"]}), success=False)
        self.assertEqual(before, self.state.read_bytes())

    def test_missing_replacement_child_cannot_silently_complete(self):
        self.init()
        self.add("A")
        self.park("A")
        self.split("A", self.plan("A", "A1", "A2"))
        state = self.read()
        state["tasks"] = [task for task in state["tasks"] if task["id"] != "A2"]
        next(task for task in state["tasks"] if task["id"] == "A1")["status"] = "done"
        self.state.write_text(json.dumps(state))
        self.cli("next", self.state, success=False)

    def test_split_requires_preserved_artifact_and_clean_worktree(self):
        self.init()
        self.add("A")
        self.cli("set", self.state, "A", "blocked", "--note", "too large")
        plan = self.plan("A", "A1", "A2")
        self.split("A", plan, success=False)
        self.park("A")
        (self.repo / "untracked.txt").write_text("do not delete")
        self.split("A", plan, success=False)
        self.assertTrue((self.repo / "untracked.txt").is_file())

    def test_split_budget_is_run_wide(self):
        self.init("--max-replans", "1")
        self.add("A")
        self.park("A")
        self.split("A", self.plan("A", "A1", "A2"))
        self.park("A1")
        self.split("A1", self.plan("A1", "A11", "A12"), success=False)
        self.assertEqual(self.output("next", self.state)["task"]["id"], "A2")

    def test_split_rejects_bad_ids_duplicate_packets_unknown_fields_and_missing_dependencies(self):
        self.init()
        self.add("A")
        self.park("A")
        for mutation in ("bad_id", "duplicate_packet", "qa_override", "missing_dependency"):
            with self.subTest(mutation=mutation):
                plan = self.plan("A", "A1", "A2")
                value = json.loads(plan.read_text())
                first, second = value["tasks"]
                if mutation == "bad_id":
                    first["id"] = "../../outside"
                elif mutation == "duplicate_packet":
                    second["packet"] = first["packet"]
                elif mutation == "qa_override":
                    first["qa_required"] = False
                else:
                    first["depends_on"] = ["missing"]
                plan.write_text(json.dumps(value))
                before = self.state.read_bytes()
                self.split("A", plan, success=False)
                self.assertEqual(before, self.state.read_bytes())

    def test_commit_checkpoint_must_be_current_clean_head(self):
        self.init()
        self.add("A")
        self.start("A")
        self.alter_task("A", status="verifying")
        self.cli("set", self.state, "A", "done", success=False)
        self.cli("set", self.state, "A", "done", "--commit", "HEAD", success=False)
        (self.repo / "tracked.txt").write_text("implementation\n")
        self.cli("set", self.state, "A", "done", "--commit", "HEAD", "--note", "changed", success=False)
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "task A")
        self.cli("set", self.state, "A", "done", "--commit", self.initial_head, success=False)
        self.cli("set", self.state, "A", "done", "--commit", "HEAD")
        self.assertEqual(self.read()["tasks"][0]["commit"], self.git("rev-parse", "HEAD"))

    def test_explicit_no_change_checkpoint_is_allowed(self):
        self.init()
        self.add("A")
        self.start("A")
        self.alter_task("A", status="verifying")
        self.cli("set", self.state, "A", "done", "--commit", "HEAD",
                 "--note", "Existing implementation already passes acceptance checks")

    def test_patch_mode_is_explicit_single_task_with_artifact(self):
        self.init("--checkpoint-mode", "patch")
        self.add("A")
        self.cli("add", self.state, "--id", "B", "--title", "B", "--packet", self.packet("B"), success=False)
        self.start("A")
        self.alter_task("A", status="verifying")
        (self.repo / "tracked.txt").write_text("uncommitted implementation\n")
        self.cli("set", self.state, "A", "done", success=False)
        artifact = self.artifact("result.json")
        self.cli("set", self.state, "A", "done", "--artifact", artifact, "--commit", "HEAD", success=False)
        self.cli("set", self.state, "A", "done", "--artifact", artifact)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.assertTrue(self.git("status", "--porcelain"))
        self.assertEqual(self.read()["tasks"][0]["checkpoint_artifact"], str(artifact.resolve()))

    def test_split_rejects_modified_preserved_artifact(self):
        self.init()
        self.add("A")
        self.park("A")
        (self.root / "A-parked.json").write_text("replaced evidence")
        self.split("A", self.plan("A", "A1", "A2"), success=False)

    def test_new_task_start_rejects_dirty_worktree_without_deleting_files(self):
        self.init()
        self.add("A")
        (self.repo / "untracked.txt").write_text("unfinished work")
        self.start("A", success=False)
        self.assertEqual((self.repo / "untracked.txt").read_text(), "unfinished work")

    def test_patch_mode_cannot_split(self):
        self.init("--checkpoint-mode", "patch")
        self.add("A")
        self.park("A")
        self.split("A", self.plan("A", "A1", "A2"), success=False)

    def test_patch_blocked_task_keeps_uncommitted_work_and_can_retry(self):
        self.init("--checkpoint-mode", "patch")
        self.add("A")
        self.start("A")
        (self.repo / "tracked.txt").write_text("partial implementation\n")
        self.park("A")
        self.assertEqual(self.output("next", self.state)["result"], "blocked")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "partial implementation\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial_head)
        self.cli("set", self.state, "A", "pending")
        self.cli("start", self.state, "A", "--output-dir", self.root / "A-second-run")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "partial implementation\n")

    def test_exhausted_task_cannot_reset_attempt_counter_by_unblocking(self):
        self.init("--max-retries", "0")
        self.add("A")
        self.start("A")
        self.park("A")
        self.cli("set", self.state, "A", "pending", success=False)

    def test_only_oversized_task_stays_active_until_explicit_replan(self):
        self.init()
        self.add("A")
        self.start("A")
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "too large")
        self.assertEqual(self.output("next", self.state)["result"], "park")
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "too large",
                 "--artifact", self.artifact())
        self.assertEqual(self.output("next", self.state)["result"], "replan")
        self.assertEqual(self.output("summary", self.state)["status"], "active")
        self.split("A", self.plan("A", "A1", "A2"))
        self.assertFalse(self.read()["tasks"][0]["replan_pending"])
        self.assertEqual(self.output("next", self.state)["result"], "ready")

    def test_replan_intent_can_be_explicitly_abandoned_without_losing_parked_artifact(self):
        self.init()
        self.add("A")
        self.start("A")
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "too large",
                 "--artifact", self.artifact())
        self.cli("set", self.state, "A", "blocked", "--note", "requires user scope decision")
        self.assertEqual(self.output("next", self.state)["result"], "blocked")
        self.assertEqual(self.output("summary", self.state)["status"], "blocked")
        task = self.read()["tasks"][0]
        self.assertFalse(task["replan_pending"])
        self.assertTrue(task["parked"])
        self.assertTrue(task["blocked_artifact"])

    def test_active_worker_is_recovered_before_another_tasks_replan(self):
        self.init("--max-retries", "0")
        self.add("A")
        self.add("B")
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "too large",
                 "--artifact", self.artifact())
        self.start("B")
        (self.repo / "tracked.txt").write_text("B is still implementing\n")
        result = self.output("next", self.state)
        self.assertEqual(result["result"], "recover")
        self.assertEqual([task["id"] for task in result["tasks"]], ["B"])
        self.cli("set", self.state, "B", "retrying", "--note", "B exhausted its own budget")
        result = self.output("next", self.state)
        self.assertEqual(result["result"], "park")
        self.assertEqual(result["task"]["id"], "B")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "B is still implementing\n")

    def test_replan_flag_requires_blocked_commit_mode_and_available_budget(self):
        self.init("--max-replans", "0")
        self.add("A")
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "too large", success=False)
        self.cli("set", self.state, "A", "pending", "--replan", "--note", "invalid flag", success=False)
        state = self.read()
        state.update(checkpoint_mode="patch", max_replans=2)
        self.state.write_text(json.dumps(state))
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "too large", success=False)

    def test_unstarted_replan_still_requests_artifact_before_split(self):
        self.init()
        self.add("A")
        self.cli("set", self.state, "A", "blocked", "--replan", "--note", "plan is too large")
        self.assertEqual(self.output("next", self.state)["result"], "park")
        self.assertEqual(self.output("summary", self.state)["status"], "active")

    def test_other_replan_consuming_last_budget_requires_explicit_abandonment(self):
        self.init("--max-replans", "1")
        for task_id in ("A", "B"):
            self.add(task_id)
            self.cli("set", self.state, task_id, "blocked", "--replan", "--note", "too large",
                     "--artifact", self.artifact(task_id + ".json"))
        self.split("A", self.plan("A", "A1", "A2"))
        self.assertEqual(self.output("next", self.state)["reason"], "replan_budget_exhausted")
        self.cli("set", self.state, "B", "blocked", "--note", "budget exhausted")
        self.assertEqual(self.output("next", self.state)["result"], "ready")

    def test_legacy_completed_state_remains_readable(self):
        self.init()
        self.add("A")
        state = self.read()
        for key in ("checkpoint_mode", "max_replans", "replan_count", "replans"):
            del state[key]
        task = state["tasks"][0]
        task["status"] = "done"
        for key in ("parked", "checkpoint_artifact", "replaces", "replaced_by", "replan_pending"):
            del task[key]
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.output("next", self.state)["result"], "complete")
        self.assertEqual(self.output("summary", self.state)["checkpoint_mode"], "commit")


if __name__ == "__main__":
    unittest.main()
