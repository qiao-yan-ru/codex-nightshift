"""Check Nightshift configuration path compatibility using only temporary files."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop_config.py"
SPEC = importlib.util.spec_from_file_location("nightshift_config_test", SCRIPT)
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)


class ConfigurationPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.user = self.root / ".claude/codex-nightshift/config.json"
        self.legacy_user = self.root / ".claude/codex-implementation-loop/config.json"
        self.project_config = self.project / ".claude/codex-nightshift.json"
        self.legacy_project = self.project / ".claude/codex-implementation-loop.json"
        for name, value in (("DEFAULT_USER_PATH", self.user), ("LEGACY_USER_PATH", self.legacy_user)):
            override = patch.object(CONFIG, name, value)
            override.start()
            self.addCleanup(override.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in tuple(os.environ):
            if name.startswith("CODEX_"):
                os.environ.pop(name)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def valid_config(self):
        value = json.loads(CONFIG.DEFAULTS_PATH.read_text())
        value["onboarding"] = {"first_run": False, "status": "complete", "completed_at": "2026-09-28T00:00:00+00:00"}
        value["qa"]["mode"] = "disabled"
        return value

    def command(self, *arguments):
        args = CONFIG.build_parser().parse_args(arguments)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            args.func(args)
        return output.getvalue().strip()

    def test_new_paths_are_defaults_without_creating_files(self):
        self.assertEqual(CONFIG.user_config_path(None), self.user)
        self.assertEqual(CONFIG.project_config_path(str(self.project)), self.project_config)
        self.assertFalse(self.user.exists())
        self.assertFalse(self.project_config.exists())

    def test_legacy_paths_are_read_without_migration_when_new_paths_absent(self):
        self.write(self.legacy_user, self.valid_config())
        self.write(self.legacy_project, {"schema_version": 1, "attempts": {"implementation_retries": 7}})
        value, sources = CONFIG.resolve_config(project=str(self.project))
        self.assertEqual(value["attempts"]["implementation_retries"], 7)
        self.assertIn(str(self.legacy_user), sources)
        self.assertIn(str(self.legacy_project), sources)
        self.assertFalse(self.user.exists())
        self.assertFalse(self.project_config.exists())

    def test_new_paths_win_without_merging_legacy_files(self):
        self.write(self.legacy_user, {"invalid": "must not be read"})
        self.write(self.legacy_project, {"invalid": "must not be read"})
        self.write(self.user, self.valid_config())
        self.write(self.project_config, {"schema_version": 1, "attempts": {"implementation_retries": 3}})
        value, sources = CONFIG.resolve_config(project=str(self.project))
        self.assertEqual(value["attempts"]["implementation_retries"], 3)
        self.assertIn(str(self.user), sources)
        self.assertIn(str(self.project_config), sources)
        self.assertNotIn(str(self.legacy_user), sources)

    def test_invalid_new_config_does_not_fall_back_to_legacy(self):
        self.write(self.legacy_user, self.valid_config())
        self.write(self.user, {"invalid": "new config needs attention"})
        with self.assertRaises(CONFIG.ConfigError):
            CONFIG.resolve_config()
        self.write(self.user, self.valid_config())
        self.write(self.legacy_project, {"schema_version": 1})
        self.write(self.project_config, {"invalid": "new project config needs attention"})
        with self.assertRaises(CONFIG.ConfigError):
            CONFIG.resolve_config(project=str(self.project))

    def test_explicit_parameter_and_environment_override_default_paths(self):
        explicit = self.root / "explicit.json"
        environment = self.root / "environment.json"
        os.environ["CODEX_LOOP_CONFIG"] = str(environment)
        os.environ["CODEX_LOOP_PROJECT_CONFIG"] = str(environment)
        self.assertEqual(CONFIG.user_config_path(str(explicit)), explicit)
        self.assertEqual(CONFIG.user_config_path(None), environment)
        self.assertEqual(CONFIG.user_config_path(None, for_write=True), environment)
        self.assertEqual(CONFIG.project_config_path(str(self.project)), environment)

    def test_configure_and_defer_write_new_path_and_preserve_legacy(self):
        self.write(self.legacy_user, self.valid_config())
        original = self.legacy_user.read_bytes()
        self.assertEqual(self.command("configure", "--qa-mode", "disabled"), str(self.user))
        self.assertEqual(self.legacy_user.read_bytes(), original)
        self.user.unlink()
        self.assertEqual(self.command("defer"), str(self.user))
        self.assertEqual(self.legacy_user.read_bytes(), original)

    def test_reset_reads_effective_legacy_preferences_and_writes_new_path(self):
        value = self.valid_config()
        value["attempts"]["implementation_retries"] = 7
        self.write(self.legacy_user, value)
        original = self.legacy_user.read_bytes()
        self.assertEqual(self.command("reset-onboarding"), str(self.user))
        updated = json.loads(self.user.read_text())
        self.assertEqual(updated["attempts"]["implementation_retries"], 7)
        self.assertEqual(updated["onboarding"]["status"], "pending")
        self.assertEqual(self.legacy_user.read_bytes(), original)

    def test_explicit_write_path_is_respected(self):
        self.assertEqual(self.command("defer", "--config", str(self.legacy_user)), str(self.legacy_user))
        self.assertFalse(self.user.exists())
        custom = self.root / "custom.json"
        os.environ["CODEX_LOOP_CONFIG"] = str(custom)
        self.assertEqual(self.command("defer"), str(custom))
        self.assertFalse(self.user.exists())


if __name__ == "__main__":
    unittest.main()
