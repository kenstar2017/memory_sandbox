"""core/bot_process.py：飞书机器人进程托管。

这里真的会起进程、真的会杀进程，但杀的只可能是本测试起的那个假机器人：
`_scan_running`（会扫到本机真在跑的机器人）在每个用例里都被换成空实现，
停止路径只认测试自己写的 pidfile。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import bot_process  # noqa: E402

# 假机器人必须叫这个名字：_is_bot 就是靠命令行里的文件名认人的
# 睡得短一点：万一用例中途崩了没收干净，这东西的命令行长得跟真机器人一样，
# 会被 _scan_running 扫到，别让它在开发机上多待
FAKE_BOT = """
import sys, time
sys.stdout.write("fake bot up\\n")
sys.stdout.flush()
time.sleep(15)
"""

DYING_BOT = """
import sys
sys.stdout.write("boom: 少了点什么\\n")
sys.exit(3)
"""

# 装死：SIGTERM 不理，只能靠 SIGKILL 收场
STUBBORN_BOT = """
import signal, time
signal.signal(signal.SIGTERM, lambda *a: None)
time.sleep(15)
"""


class BotProcessBase(unittest.TestCase):
    script_body = FAKE_BOT

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pid_file = root / "feishu_bot.pid"
        self.log_file = root / "feishu_bot.log"
        self.script = root / bot_process.SCRIPT_NAME
        self.script.write_text(self.script_body, encoding="utf-8")

        self._saved = {
            name: getattr(bot_process, name)
            for name in (
                "pid_path",
                "log_path",
                "script_path",
                "_scan_running",
                "_config_summary",
                "_sdk_installed",
                "START_PROBE_SECONDS",
                "STOP_WAIT_SECONDS",
            )
        }
        bot_process.pid_path = lambda: self.pid_file
        bot_process.log_path = lambda: self.log_file
        bot_process.script_path = lambda: self.script
        # 别去碰本机真在跑的机器人
        bot_process._scan_running = lambda: []
        bot_process._config_summary = lambda: (True, 2, True, "")
        bot_process._sdk_installed = lambda: True
        bot_process.START_PROBE_SECONDS = 0.4
        bot_process.STOP_WAIT_SECONDS = 1.5
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            setattr(bot_process, name, value)
        bot_process._CHILD = None
        # 兜底：用例中途失败也不能留下野进程
        pid = self._pidfile_pid()
        if pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.tmp.cleanup()

    def _pidfile_pid(self) -> int:
        try:
            return int(json.loads(self.pid_file.read_text(encoding="utf-8"))["pid"])
        except Exception:  # noqa: BLE001
            return 0


class LifecycleTests(BotProcessBase):
    def test_start_then_stop(self):
        res = bot_process.start()
        self.assertTrue(res.ok, res.message)
        self.assertTrue(res.status.running)
        self.assertTrue(res.status.owned)
        pid = res.status.pid
        self.assertGreater(pid, 0)
        self.assertEqual(self._pidfile_pid(), pid)

        # 独立算一遍状态：不是靠 start 的返回值自说自话
        self.assertTrue(bot_process.status().running)

        res = bot_process.stop()
        self.assertTrue(res.ok, res.message)
        self.assertFalse(res.status.running)
        self.assertFalse(self.pid_file.exists())
        self.assertFalse(bot_process._alive(pid))

    def test_start_is_idempotent(self):
        first = bot_process.start()
        self.addCleanup(bot_process.stop)
        second = bot_process.start()
        self.assertTrue(second.ok)
        self.assertIn("已经在跑", second.message)
        self.assertEqual(second.status.pid, first.status.pid)

    def test_restart_replaces_the_process(self):
        first = bot_process.start()
        self.addCleanup(bot_process.stop)
        res = bot_process.restart()
        self.assertTrue(res.ok, res.message)
        self.assertTrue(res.status.running)
        self.assertNotEqual(res.status.pid, first.status.pid)
        self.assertFalse(bot_process._alive(first.status.pid))

    def test_stop_when_nothing_runs(self):
        res = bot_process.stop()
        self.assertTrue(res.ok)
        self.assertIn("没在跑", res.message)

    def test_survives_the_parent_going_away(self):
        # 机器人自成会话：这里只验证它确实换了进程组，
        # 否则 API 服务被 Rust 整组杀掉时会把机器人一起带走
        res = bot_process.start()
        self.addCleanup(bot_process.stop)
        self.assertEqual(os.getpgid(res.status.pid), res.status.pid)
        self.assertNotEqual(os.getpgid(res.status.pid), os.getpgid(os.getpid()))

    def test_log_is_captured(self):
        bot_process.start()
        self.addCleanup(bot_process.stop)
        deadline = time.time() + 3
        while time.time() < deadline and "fake bot up" not in bot_process.tail_log():
            time.sleep(0.1)
        self.assertIn("fake bot up", bot_process.tail_log())


class StubbornBotTests(BotProcessBase):
    script_body = STUBBORN_BOT

    def test_sigterm_is_ignored_so_it_gets_killed(self):
        res = bot_process.start()
        self.assertTrue(res.ok, res.message)
        pid = res.status.pid
        res = bot_process.stop()
        self.assertTrue(res.ok, res.message)
        self.assertFalse(bot_process._is_bot(pid))


class DuplicateInstanceTests(BotProcessBase):
    def test_stop_also_kills_the_one_started_elsewhere(self):
        # 终端里起过一个、界面又起了一个：同一条消息会被回两遍，
        # 所以按停止得把两个都收掉
        first = bot_process.start()
        stray = subprocess.Popen(
            [sys.executable, str(self.script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(_kill_quietly, stray)
        bot_process._scan_running = lambda: [stray.pid]

        res = bot_process.stop()
        self.assertTrue(res.ok, res.message)
        self.assertIn("重复实例", res.message)
        self.assertFalse(bot_process._is_bot(first.status.pid))
        self.assertFalse(bot_process._is_bot(stray.pid))


def _kill_quietly(proc: "subprocess.Popen") -> None:
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        pass


class EarlyExitTests(BotProcessBase):
    script_body = DYING_BOT

    def test_start_reports_the_reason_it_died(self):
        res = bot_process.start()
        self.assertFalse(res.ok)
        self.assertIn("退出码 3", res.message)
        # 关键是把日志尾巴带出来：不然界面上只有一句「启动失败」，人还得去翻文件
        self.assertIn("boom", res.message)
        self.assertFalse(res.status.running)
        self.assertFalse(self.pid_file.exists())


class StalePidTests(BotProcessBase):
    def test_recycled_pid_is_not_mistaken_for_the_bot(self):
        # 当前测试进程肯定活着，但它不是机器人；只认 os.kill(pid, 0) 就会误判
        self.pid_file.write_text(
            json.dumps({"pid": os.getpid(), "started_at": "2026-01-01 00:00:00"}),
            encoding="utf-8",
        )
        st = bot_process.status()
        self.assertFalse(st.running)
        self.assertFalse(self.pid_file.exists(), "陈掉的 pidfile 应该被清掉")

    def test_dead_pid_is_cleaned_up(self):
        self.pid_file.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
        self.assertFalse(bot_process.status().running)
        self.assertFalse(self.pid_file.exists())

    def test_broken_pidfile_does_not_explode(self):
        self.pid_file.write_text("{ 这不是 json", encoding="utf-8")
        self.assertFalse(bot_process.status().running)


class PreflightTests(BotProcessBase):
    def test_missing_script_refuses_to_start(self):
        self.script.unlink()
        res = bot_process.start()
        self.assertFalse(res.ok)
        self.assertIn(bot_process.SCRIPT_NAME, res.message)
        self.assertFalse(res.status.available)

    def test_missing_sdk_refuses_to_start(self):
        bot_process._sdk_installed = lambda: False
        res = bot_process.start()
        self.assertFalse(res.ok)
        self.assertIn("lark-oapi", res.message)

    def test_unconfigured_refuses_to_start(self):
        bot_process._config_summary = lambda: (False, 0, False, "")
        res = bot_process.start()
        self.assertFalse(res.ok)
        self.assertIn("app_id", res.message)

    def test_status_surfaces_config_summary(self):
        st = bot_process.status()
        self.assertEqual(st.allow_count, 2)
        self.assertTrue(st.doc_bot_enabled)
        self.assertTrue(st.configured)
        self.assertEqual(st.script, str(self.script))


class ConfigSummaryTests(unittest.TestCase):
    def test_reads_the_real_config_without_raising(self):
        configured, allow, doc_bot, err = bot_process._config_summary()
        self.assertIsInstance(configured, bool)
        self.assertIsInstance(allow, int)
        self.assertIsInstance(doc_bot, bool)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
