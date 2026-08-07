"""hook 脚本的行为测试：真的当子进程跑，喂 stdin、读 stdout。

只测安装器不够——真正决定「知识有没有到模型」的是这两个脚本的判断逻辑。
用临时 HOME 隔离状态目录（脚本里是 Path.home()，POSIX 下认 $HOME），
用一个本地假后端替代记忆沙箱 API。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "cursor_hooks"
PREFETCH = HOOKS / "memory-prefetch.py"
GATE = HOOKS / "memory-require-prepare.py"
MARK = HOOKS / "memory-mark.py"
ENSURE = HOOKS / "memory-ensure-remember.py"

PACK_TEXT = "【记忆沙箱 · 参考问答】\n### 参考问答 1\n问：zIndex 怎么治理\n答：用 Z_INDEX 常量表。"


class _FakeApi(BaseHTTPRequestHandler):
    """假的 /api/prepare。pack 内容与请求计数由类变量控制。"""

    pack = PACK_TEXT
    calls: list = []

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(body)
        payload = json.dumps(
            {"ok": True, "context_pack": type(self).pack, "reference_count": 1}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class HookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = self.home / ".cursor" / "memory-sandbox-hook-state"
        _FakeApi.calls = []
        _FakeApi.pack = PACK_TEXT
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _run(
        self,
        script: Path,
        payload: dict,
        api: str | None = None,
        env_extra: dict | None = None,
    ) -> dict:
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "MEMORY_SANDBOX_API": api if api is not None else self.api,
        }
        env.update(env_extra or {})
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "{}")

    def _marker(self, conv: str, suffix: str) -> Path:
        return self.state / f"{conv}.{suffix}"

    def _touch(self, conv: str, suffix: str, text: str = "1") -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self._marker(conv, suffix).write_text(text, encoding="utf-8")


class PrefetchTests(HookTestCase):
    def test_stores_pack_and_never_blocks(self):
        out = self._run(
            PREFETCH, {"conversation_id": "c1", "prompt": "zIndex 层级怎么治理"}
        )
        self.assertTrue(out["continue"])
        self.assertEqual(
            self._marker("c1", "pack").read_text(encoding="utf-8"), PACK_TEXT
        )

    def test_query_is_the_user_prompt(self):
        """预取的价值就在于用原话，而不是 agent 转述的检索词。"""
        self._run(PREFETCH, {"conversation_id": "c1", "prompt": "客服 Popover 层级"})
        self.assertEqual(_FakeApi.calls[0]["query"], "客服 Popover 层级")

    def test_no_relevant_memory_marks_turn_done(self):
        """记忆里没有相关内容就一次都不该拦。"""
        _FakeApi.pack = ""
        self._run(PREFETCH, {"conversation_id": "c1", "prompt": "随便问点什么东西"})
        self.assertFalse(self._marker("c1", "pack").exists())
        self.assertTrue(self._marker("c1", "prepared").is_file())

    def test_backend_down_fails_open(self):
        out = self._run(
            PREFETCH,
            {"conversation_id": "c1", "prompt": "后端没起来的时候"},
            api="http://127.0.0.1:9",
        )
        self.assertTrue(out["continue"])
        self.assertFalse(self._marker("c1", "pack").exists())
        self.assertFalse(self._marker("c1", "prepared").exists())

    def test_new_turn_clears_previous_markers(self):
        self._touch("c1", "prepared")
        self._touch("c1", "gated")
        _FakeApi.pack = ""
        self._run(PREFETCH, {"conversation_id": "c1", "prompt": "新的一轮提问"})
        self.assertFalse(self._marker("c1", "gated").exists())
        # prepared 是这轮重新写的（无相关记忆），不是上一轮残留
        self.assertEqual(
            self._marker("c1", "prepared").read_text(encoding="utf-8"), "prefetch"
        )

    def test_trivial_prompt_skips_retrieval(self):
        out = self._run(PREFETCH, {"conversation_id": "c1", "prompt": "好"})
        self.assertTrue(out["continue"])
        self.assertEqual(_FakeApi.calls, [])

    def test_garbage_stdin_still_continues(self):
        proc = subprocess.run(
            [sys.executable, str(PREFETCH)],
            input="not json",
            capture_output=True,
            text=True,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(json.loads(proc.stdout)["continue"])


class GateDeliveryTests(HookTestCase):
    def test_delivers_pack_to_model_on_first_tool(self):
        self._touch("c1", "pack", PACK_TEXT)
        out = self._run(GATE, {"conversation_id": "c1", "tool_name": "Read"})
        self.assertEqual(out["permission"], "deny")
        self.assertIn(PACK_TEXT, out["agent_message"])
        self.assertIn("预检索", out["agent_message"])

    def test_delivery_asks_agent_to_fix_stale_memories(self):
        """光把记忆交出去不够：过时的那条得让它顺手改掉。"""
        self._touch("c1", "pack", PACK_TEXT)
        out = self._run(GATE, {"conversation_id": "c1", "tool_name": "Read"})
        msg = out["agent_message"]
        self.assertIn("memory_update", msg)
        self.assertIn("memory_delete", msg)

    def test_delivery_marks_turn_done_and_consumes_pack(self):
        self._touch("c1", "pack", PACK_TEXT)
        self._run(GATE, {"conversation_id": "c1", "tool_name": "Read"})
        self.assertTrue(self._marker("c1", "prepared").is_file())
        self.assertFalse(self._marker("c1", "pack").exists())

    def test_second_tool_allowed_after_delivery(self):
        self._touch("c1", "pack", PACK_TEXT)
        self._run(GATE, {"conversation_id": "c1", "tool_name": "Read"})
        out = self._run(GATE, {"conversation_id": "c1", "tool_name": "Write"})
        self.assertEqual(out["permission"], "allow")

    def test_tells_agent_to_search_when_no_pack(self):
        out = self._run(GATE, {"conversation_id": "c1", "tool_name": "Write"})
        self.assertEqual(out["permission"], "deny")
        self.assertIn("memory_prepare", out["agent_message"])

    def test_read_allowed_when_no_pack(self):
        """没预取到东西时不拦读，保持探索速度。"""
        out = self._run(GATE, {"conversation_id": "c1", "tool_name": "Read"})
        self.assertEqual(out["permission"], "allow")

    def test_memory_tools_always_allowed(self):
        self._touch("c1", "pack", PACK_TEXT)
        for tool in ("MCP:memory_prepare", "MCP:memory_ask"):
            out = self._run(GATE, {"conversation_id": "c1", "tool_name": tool})
            self.assertEqual(out["permission"], "allow", tool)
        # 放行不能顺手把包吃掉，否则真正动手时就没得投递了
        self.assertTrue(self._marker("c1", "pack").is_file())

    def test_prepared_marker_short_circuits(self):
        self._touch("c1", "prepared")
        self._touch("c1", "pack", PACK_TEXT)
        out = self._run(GATE, {"conversation_id": "c1", "tool_name": "Write"})
        self.assertEqual(out["permission"], "allow")

    def test_only_one_denial_per_turn(self):
        """MCP 挂了、agent 又坚持要做时不能把整轮卡死。"""
        first = self._run(GATE, {"conversation_id": "c1", "tool_name": "Write"})
        self.assertEqual(first["permission"], "deny")
        second = self._run(GATE, {"conversation_id": "c1", "tool_name": "Write"})
        self.assertEqual(second["permission"], "allow")

    def test_feishu_write_gated_without_pack(self):
        out = self._run(
            GATE, {"conversation_id": "c1", "tool_name": "MCP:memory_feishu_edit_body"}
        )
        self.assertEqual(out["permission"], "deny")

    def test_garbage_stdin_allows(self):
        proc = subprocess.run(
            [sys.executable, str(GATE)],
            input="not json",
            capture_output=True,
            text=True,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["permission"], "allow")

    def test_subagent_gets_its_own_turn(self):
        """子 agent 是新的 conversation_id，父会话的标记不该放行它。"""
        self._touch("parent", "prepared")
        out = self._run(GATE, {"conversation_id": "child", "tool_name": "Write"})
        self.assertEqual(out["permission"], "deny")


class MarkTests(HookTestCase):
    """postToolUse 记账：哪些工具算「查过」，哪些算「落过库」。"""

    def _mark(self, tool: str) -> None:
        self._run(MARK, {"conversation_id": "c1", "tool_name": tool})

    def test_prepare_marks_prepared(self):
        self._mark("MCP:memory_prepare")
        self.assertTrue(self._marker("c1", "prepared").is_file())
        self.assertFalse(self._marker("c1", "remembered").exists())

    def test_remember_marks_remembered(self):
        self._mark("MCP:memory_remember")
        self.assertTrue(self._marker("c1", "remembered").is_file())

    def test_update_counts_as_recording(self):
        """改掉过时结论就是本轮该落的库，不该再被追着写一条新的。"""
        self._mark("MCP:memory_update")
        self.assertTrue(self._marker("c1", "remembered").is_file())

    def test_delete_counts_as_recording(self):
        self._mark("MCP:memory_delete")
        self.assertTrue(self._marker("c1", "remembered").is_file())

    def test_unrelated_tool_marks_nothing(self):
        self._mark("Read")
        self.assertFalse(self._marker("c1", "remembered").exists())
        self.assertFalse(self._marker("c1", "prepared").exists())

    def test_feishu_write_is_not_recording(self):
        """飞书写操作自动落的只是操作流水，不等于记下了本轮结论。"""
        self._mark("MCP:memory_feishu_edit_body")
        self.assertFalse(self._marker("c1", "remembered").exists())

    def test_garbage_stdin_is_silent(self):
        proc = subprocess.run(
            [sys.executable, str(MARK)],
            input="not json",
            capture_output=True,
            text=True,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {})


class EnsureRememberTests(HookTestCase):
    """stop 兜底：没落库才追问，落过就放行。"""

    def _stop(self, conv: str = "c1") -> dict:
        return self._run(ENSURE, {"conversation_id": conv})

    def test_asks_when_nothing_recorded(self):
        self.assertIn("落库兜底", self._stop().get("followup_message", ""))

    def test_silent_after_update(self):
        """本轮用 memory_update 修了旧条目，就不该再被追问。"""
        self._run(MARK, {"conversation_id": "c1", "tool_name": "MCP:memory_update"})
        self.assertEqual(self._stop(), {})

    def test_marker_cleared_so_next_turn_is_judged_again(self):
        self._run(MARK, {"conversation_id": "c1", "tool_name": "MCP:memory_remember"})
        self._stop()
        self.assertFalse(self._marker("c1", "remembered").exists())
        self.assertIn("落库兜底", self._stop().get("followup_message", ""))


class NestedAgentTests(HookTestCase):
    """
    机器人把本机 agent CLI 当模型用时，那个嵌套 agent 不该走这套门禁。

    它带 --approve-mcps，所以也有记忆沙箱 MCP、也继承这些 hook。以前 stop 门禁会
    逼它自己 memory_remember 一条，而机器人随后又写一条——一次评论问答落两条，
    标题、tags 还各不相同（实测 2026-08-07 16:22 那两条）。
    """

    NESTED = {"MEMORY_SANDBOX_NESTED": "1"}

    def test_stop_gate_does_not_force_it_to_record(self):
        out = self._run(ENSURE, {"conversation_id": "n1"}, env_extra=self.NESTED)
        self.assertEqual(out, {}, "嵌套 agent 被追问就会自己写一条，于是落两条")

    def test_read_gate_lets_it_work(self):
        self._touch("n1", "pack", PACK_TEXT)
        out = self._run(
            GATE,
            {"conversation_id": "n1", "tool_name": "Write"},
            env_extra=self.NESTED,
        )
        self.assertEqual(out["permission"], "allow")

    def test_it_does_not_prefetch_again(self):
        out = self._run(
            PREFETCH,
            {"conversation_id": "n1", "prompt": "这段的口径是什么"},
            env_extra=self.NESTED,
        )
        self.assertTrue(out["continue"])
        self.assertEqual(_FakeApi.calls, [], "机器人已经检索过并把参考塞进上下文了")

    def test_it_leaves_no_markers_behind(self):
        self._run(
            MARK,
            {"conversation_id": "n1", "tool_name": "MCP:memory_remember"},
            env_extra=self.NESTED,
        )
        self.assertFalse(self._marker("n1", "remembered").exists())

    def test_the_protocol_is_not_injected_into_it(self):
        # 那套协议会诱导它自己去查记忆、写记忆、动飞书；它只该产出一段文本
        out = self._run(
            HOOKS / "memory-session-context.py", {}, env_extra=self.NESTED
        )
        self.assertEqual(out, {})

    def test_normal_sessions_still_get_everything(self):
        self.assertIn("落库兜底", self._run(ENSURE, {"conversation_id": "c1"}).get(
            "followup_message", ""
        ))
        self.assertIn(
            "记忆沙箱调用协议",
            self._run(HOOKS / "memory-session-context.py", {})["additional_context"],
        )

    def test_the_bot_marks_the_agent_it_spawns(self):
        """没有这个标记，上面几条放行就永远不会发生。"""
        import os
        from unittest.mock import patch

        from core.config import LLMConfig
        from core.llm import CursorLocalAgentLLM

        llm = CursorLocalAgentLLM(LLMConfig(provider="cursor", timeout=30))
        llm.agent_bin = "/bin/echo"
        llm.cwd = str(REPO)
        captured = {}

        class _Proc:
            def communicate(self, timeout=None):
                return "答案", ""

            returncode = 0

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return _Proc()

        with patch("subprocess.Popen", side_effect=fake_popen), patch.dict(
            os.environ, {}, clear=False
        ):
            llm.generate("这段的口径是什么")

        self.assertEqual(captured["env"].get("MEMORY_SANDBOX_NESTED"), "1")


if __name__ == "__main__":
    unittest.main()
