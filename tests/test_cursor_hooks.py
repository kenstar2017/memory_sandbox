"""Cursor hook 安装器测试（标准库 unittest）。

重点是「不能毁掉用户已有配置」：合并、幂等、升级、卸载、坏配置。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cursor_hooks  # noqa: E402


class CursorHooksTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor = Path(self.tmp.name) / ".cursor"
        self._prev = os.environ.get("MEMORY_SANDBOX_CURSOR_DIR")
        os.environ["MEMORY_SANDBOX_CURSOR_DIR"] = str(self.cursor)
        self.python = sys.executable

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("MEMORY_SANDBOX_CURSOR_DIR", None)
        else:
            os.environ["MEMORY_SANDBOX_CURSOR_DIR"] = self._prev
        self.tmp.cleanup()

    def _hooks_json(self) -> dict:
        return json.loads(cursor_hooks.hooks_json_path().read_text(encoding="utf-8"))

    def _write_hooks_json(self, data: dict) -> None:
        path = cursor_hooks.hooks_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class InstallTests(CursorHooksTestCase):
    def test_install_from_scratch(self):
        res = cursor_hooks.install(python=self.python)
        self.assertTrue(res.ok, res.error)

        data = self._hooks_json()
        self.assertEqual(data["version"], 1)
        self.assertEqual(
            sorted(data["hooks"].keys()), sorted(cursor_hooks.HOOK_SPECS.keys())
        )
        for name in cursor_hooks.SCRIPT_NAMES:
            self.assertTrue((cursor_hooks.hooks_dir() / name).is_file(), name)

    def test_scripts_are_executable_and_identical_to_source(self):
        cursor_hooks.install(python=self.python)
        src = cursor_hooks.source_dir()
        assert src is not None
        for name in cursor_hooks.SCRIPT_NAMES:
            installed = cursor_hooks.hooks_dir() / name
            self.assertEqual(installed.read_bytes(), (src / name).read_bytes())
            self.assertTrue(os.access(installed, os.X_OK), name)

    def test_entries_carry_matcher_and_loop_limit(self):
        cursor_hooks.install(python=self.python)
        hooks = self._hooks_json()["hooks"]
        self.assertEqual(hooks["preToolUse"][0]["matcher"], "Write|Delete|Task|MCP:")
        self.assertEqual(hooks["postToolUse"][0]["matcher"], "MCP:")
        self.assertEqual(hooks["stop"][0]["loop_limit"], 1)
        # sessionStart 不该带 matcher（它没有可匹配的工具）
        self.assertNotIn("matcher", hooks["sessionStart"][0])

    def test_command_uses_absolute_paths(self):
        cursor_hooks.install(python=self.python)
        command = self._hooks_json()["hooks"]["stop"][0]["command"]
        self.assertIn(str(cursor_hooks.hooks_dir()), command)
        self.assertIn(self.python, command)

    def test_command_quotes_paths_with_spaces(self):
        spaced = Path(self.tmp.name) / "My Cursor" / ".cursor"
        os.environ["MEMORY_SANDBOX_CURSOR_DIR"] = str(spaced)
        res = cursor_hooks.install(python=self.python)
        self.assertTrue(res.ok, res.error)
        command = self._hooks_json()["hooks"]["stop"][0]["command"]
        self.assertIn("'", command)
        self.assertIn("My Cursor", command)

    def test_manifest_written(self):
        cursor_hooks.install(python=self.python)
        manifest = json.loads(
            (cursor_hooks.hooks_dir() / cursor_hooks.MANIFEST_NAME).read_text("utf-8")
        )
        self.assertEqual(manifest["python"], self.python)
        self.assertTrue(manifest["installed_at"])


class MergeTests(CursorHooksTestCase):
    def test_preserves_user_hooks_in_other_events(self):
        self._write_hooks_json(
            {
                "version": 1,
                "hooks": {
                    "afterFileEdit": [{"command": "./my-formatter.sh"}],
                    "beforeShellExecution": [
                        {"command": "./approve.sh", "matcher": "curl"}
                    ],
                },
            }
        )
        res = cursor_hooks.install(python=self.python)
        self.assertTrue(res.ok, res.error)

        hooks = self._hooks_json()["hooks"]
        self.assertEqual(hooks["afterFileEdit"], [{"command": "./my-formatter.sh"}])
        self.assertEqual(
            hooks["beforeShellExecution"], [{"command": "./approve.sh", "matcher": "curl"}]
        )
        self.assertEqual(res.kept_foreign, 2)

    def test_preserves_user_hooks_inside_same_event(self):
        self._write_hooks_json(
            {
                "version": 1,
                "hooks": {"stop": [{"command": "./my-own-stop.sh", "loop_limit": 3}]},
            }
        )
        cursor_hooks.install(python=self.python)

        stop = self._hooks_json()["hooks"]["stop"]
        self.assertEqual(len(stop), 2)
        self.assertEqual(stop[0], {"command": "./my-own-stop.sh", "loop_limit": 3})
        self.assertIn("memory-ensure-remember.py", stop[1]["command"])

    def test_preserves_unknown_root_keys(self):
        self._write_hooks_json({"version": 1, "hooks": {}, "customField": {"a": 1}})
        cursor_hooks.install(python=self.python)
        self.assertEqual(self._hooks_json()["customField"], {"a": 1})

    def test_does_not_overwrite_existing_version(self):
        self._write_hooks_json({"version": 2, "hooks": {}})
        cursor_hooks.install(python=self.python)
        self.assertEqual(self._hooks_json()["version"], 2)

    def test_backup_created_when_file_existed(self):
        self._write_hooks_json({"version": 1, "hooks": {}})
        res = cursor_hooks.install(python=self.python)
        self.assertTrue(res.backup)
        self.assertTrue(Path(res.backup).is_file())

    def test_no_backup_when_nothing_to_lose(self):
        res = cursor_hooks.install(python=self.python)
        self.assertEqual(res.backup, "")

    def test_malformed_json_is_refused_without_clobbering(self):
        path = cursor_hooks.hooks_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        res = cursor_hooks.install(python=self.python)
        self.assertFalse(res.ok)
        self.assertIn("合法 JSON", res.error)
        self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")

    def test_non_object_root_is_refused(self):
        path = cursor_hooks.hooks_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1,2,3]", encoding="utf-8")
        res = cursor_hooks.install(python=self.python)
        self.assertFalse(res.ok)
        self.assertIn("顶层不是对象", res.error)

    def test_tolerates_garbage_entry_shapes(self):
        self._write_hooks_json(
            {"version": 1, "hooks": {"stop": "not-a-list", "preToolUse": [None, 42]}}
        )
        res = cursor_hooks.install(python=self.python)
        self.assertTrue(res.ok, res.error)
        hooks = self._hooks_json()["hooks"]
        self.assertTrue(any(_ours(e) for e in hooks["stop"]))


class IdempotencyTests(CursorHooksTestCase):
    def test_reinstall_does_not_duplicate(self):
        cursor_hooks.install(python=self.python)
        cursor_hooks.install(python=self.python)
        cursor_hooks.install(python=self.python)

        hooks = self._hooks_json()["hooks"]
        for event in cursor_hooks.HOOK_SPECS:
            ours = [e for e in hooks[event] if _ours(e)]
            self.assertEqual(len(ours), 1, f"{event} 出现重复条目")

    def test_changing_python_replaces_entry_not_appends(self):
        cursor_hooks.install(python="/usr/bin/python3")
        cursor_hooks.install(python=self.python)

        stop = self._hooks_json()["hooks"]["stop"]
        self.assertEqual(len(stop), 1)
        self.assertIn(self.python, stop[0]["command"])

    def test_entry_moved_to_another_event_leaves_no_residue(self):
        cursor_hooks.install(python=self.python)
        # 模拟老版本把某个脚本挂在别的事件上
        data = self._hooks_json()
        data["hooks"]["subagentStop"] = [
            {"command": f"python3 {cursor_hooks.hooks_dir()}/memory-mark.py"}
        ]
        self._write_hooks_json(data)

        cursor_hooks.install(python=self.python)
        self.assertNotIn("subagentStop", self._hooks_json()["hooks"])


class StatusTests(CursorHooksTestCase):
    def test_not_installed(self):
        st = cursor_hooks.status()
        self.assertFalse(st.installed)
        self.assertEqual(sorted(st.missing_scripts), sorted(cursor_hooks.SCRIPT_NAMES))
        self.assertEqual(
            sorted(st.missing_events), sorted(cursor_hooks.HOOK_SPECS.keys())
        )

    def test_installed_and_up_to_date(self):
        cursor_hooks.install(python=self.python)
        st = cursor_hooks.status()
        self.assertTrue(st.installed)
        self.assertTrue(st.up_to_date)
        self.assertFalse(st.stale_scripts)
        self.assertTrue(st.installed_at)

    def test_outdated_script_detected_by_hash(self):
        cursor_hooks.install(python=self.python)
        target = cursor_hooks.hooks_dir() / "memory-mark.py"
        target.write_text("# 旧版本\n", encoding="utf-8")

        st = cursor_hooks.status()
        self.assertTrue(st.installed)
        self.assertFalse(st.up_to_date)
        self.assertEqual(st.stale_scripts, ["memory-mark.py"])

    def test_reinstall_refreshes_stale_script(self):
        cursor_hooks.install(python=self.python)
        (cursor_hooks.hooks_dir() / "memory-mark.py").write_text("# 旧", encoding="utf-8")
        cursor_hooks.install(python=self.python)
        self.assertTrue(cursor_hooks.status().up_to_date)

    def test_entry_removed_by_hand_is_reported(self):
        cursor_hooks.install(python=self.python)
        data = self._hooks_json()
        data["hooks"].pop("preToolUse")
        self._write_hooks_json(data)

        st = cursor_hooks.status()
        self.assertFalse(st.installed)
        self.assertEqual(st.missing_events, ["preToolUse"])

    def test_counts_foreign_entries(self):
        self._write_hooks_json(
            {"version": 1, "hooks": {"afterFileEdit": [{"command": "./fmt.sh"}]}}
        )
        cursor_hooks.install(python=self.python)
        self.assertEqual(cursor_hooks.status().foreign_entries, 1)

    def test_malformed_json_reported_not_raised(self):
        path = cursor_hooks.hooks_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("nope", encoding="utf-8")
        st = cursor_hooks.status()
        self.assertFalse(st.installed)
        self.assertIn("合法 JSON", st.error)


class UninstallTests(CursorHooksTestCase):
    def test_removes_ours_keeps_foreign(self):
        self._write_hooks_json(
            {
                "version": 1,
                "hooks": {
                    "stop": [{"command": "./my-own-stop.sh"}],
                    "afterFileEdit": [{"command": "./fmt.sh"}],
                },
            }
        )
        cursor_hooks.install(python=self.python)
        res = cursor_hooks.uninstall()
        self.assertTrue(res.ok, res.error)

        hooks = self._hooks_json()["hooks"]
        self.assertEqual(hooks["stop"], [{"command": "./my-own-stop.sh"}])
        self.assertEqual(hooks["afterFileEdit"], [{"command": "./fmt.sh"}])
        for event in ("sessionStart", "preToolUse", "postToolUse"):
            self.assertNotIn(event, hooks)

    def test_removes_scripts_manifest_and_state(self):
        cursor_hooks.install(python=self.python)
        state = cursor_hooks.state_dir()
        state.mkdir(parents=True, exist_ok=True)
        (state / "conv.prepared").write_text("1", encoding="utf-8")

        cursor_hooks.uninstall()
        for name in cursor_hooks.SCRIPT_NAMES:
            self.assertFalse((cursor_hooks.hooks_dir() / name).exists(), name)
        self.assertFalse((cursor_hooks.hooks_dir() / cursor_hooks.MANIFEST_NAME).exists())
        self.assertFalse(state.exists())

    def test_uninstall_when_never_installed_is_noop(self):
        res = cursor_hooks.uninstall()
        self.assertTrue(res.ok, res.error)

    def test_status_after_uninstall(self):
        cursor_hooks.install(python=self.python)
        cursor_hooks.uninstall()
        self.assertFalse(cursor_hooks.status().installed)


class PythonResolutionTests(unittest.TestCase):
    def test_resolves_to_existing_interpreter(self):
        python = cursor_hooks.resolve_python()
        self.assertTrue(Path(python).exists() or python == "python3")

    def test_env_override_wins(self):
        prev = os.environ.get("MEMORY_SANDBOX_HOOK_PYTHON")
        os.environ["MEMORY_SANDBOX_HOOK_PYTHON"] = sys.executable
        try:
            self.assertEqual(cursor_hooks.resolve_python(), sys.executable)
        finally:
            if prev is None:
                os.environ.pop("MEMORY_SANDBOX_HOOK_PYTHON", None)
            else:
                os.environ["MEMORY_SANDBOX_HOOK_PYTHON"] = prev

    def test_nonexistent_override_ignored(self):
        prev = os.environ.get("MEMORY_SANDBOX_HOOK_PYTHON")
        os.environ["MEMORY_SANDBOX_HOOK_PYTHON"] = "/nope/python3"
        try:
            self.assertNotEqual(cursor_hooks.resolve_python(), "/nope/python3")
        finally:
            if prev is None:
                os.environ.pop("MEMORY_SANDBOX_HOOK_PYTHON", None)
            else:
                os.environ["MEMORY_SANDBOX_HOOK_PYTHON"] = prev


class SourceTests(unittest.TestCase):
    def test_repo_ships_all_hook_scripts(self):
        src = cursor_hooks.source_dir()
        self.assertIsNotNone(src, "仓库里必须有 cursor_hooks/，否则装不到用户机器上")
        assert src is not None
        for name in cursor_hooks.SCRIPT_NAMES:
            self.assertTrue((src / name).is_file(), name)

    def test_hook_scripts_are_stdlib_only(self):
        """hook 要在任意机器上跑，不能 import 第三方库。"""
        src = cursor_hooks.source_dir()
        assert src is not None
        allowed = {
            "__future__",
            "json",
            "sys",
            "time",
            "pathlib",
            "os",
            "hashlib",
            "shutil",
        }
        for name in cursor_hooks.SCRIPT_NAMES:
            for line in (src / name).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(("import ", "from ")):
                    module = line.split()[1].split(".")[0]
                    self.assertIn(module, allowed, f"{name} 引入了 {module}")


def _ours(entry) -> bool:
    return cursor_hooks._is_ours(entry)


if __name__ == "__main__":
    unittest.main()
