"""落库门禁（sop_gate）测试：prepare 记账、remember/update/delete 清零。

门禁状态是 mcp_server 模块级变量，测试间需手动复位；call_tool 链路用假沙箱，
不触盘、不调用真实记忆。
"""

import json
import types
import unittest

import mcp_server


class _FakeLongTerm:
    def reload(self):
        pass

    def search_hits(self, *args, **kwargs):
        return []


class _FakeSandbox:
    def __init__(self):
        self.long_term = _FakeLongTerm()
        self.working = types.SimpleNamespace(scene="dev")
        self.remembered = []
        self.updated = []
        self.deleted = []

    def build_reference_pack(self, *args, **kwargs):
        return {"references": [], "context_pack": "", "ref_threshold": 0.45}

    def remember(self, q, a, **kwargs):
        self.remembered.append((q, a))
        return "已写入长时记忆 [faketest]（场景: dev）"

    def find_memory(self, **kwargs):
        return None

    def update_memory(self, **kwargs):
        self.updated.append(kwargs)
        return "已更新记忆 [faketest]"

    def delete_memory(self, **kwargs):
        self.deleted.append(kwargs)
        return "已删除记忆 [faketest]"


def _fake_ask_payload(query, tags=None, ref_top_k=5):
    return {
        "answer": "",
        "source": "miss",
        "hit_local": False,
        "hits": [],
        "explain": [],
        "references": [],
    }


def _result_text(result):
    return result["content"][0]["text"]


class SopGateStateTests(unittest.TestCase):
    def setUp(self):
        mcp_server._PREPARES_SINCE_WRITE = 0

    def test_first_prepare_no_warning(self):
        self.assertEqual(mcp_server._sop_gate_on_prepare(), "")

    def test_second_prepare_without_write_warns(self):
        mcp_server._sop_gate_on_prepare()
        warn = mcp_server._sop_gate_on_prepare()
        self.assertIn("落库门禁", warn)
        self.assertIn("1 轮", warn)
        self.assertIn("memory_remember", warn)

    def test_write_resets_counter(self):
        mcp_server._sop_gate_on_prepare()
        mcp_server._sop_gate_on_prepare()
        mcp_server._sop_gate_on_write()
        self.assertEqual(mcp_server._sop_gate_on_prepare(), "")

    def test_missed_count_accumulates(self):
        mcp_server._sop_gate_on_prepare()
        mcp_server._sop_gate_on_prepare()
        warn = mcp_server._sop_gate_on_prepare()
        self.assertIn("2 轮", warn)


class SopGateCallToolTests(unittest.TestCase):
    def setUp(self):
        mcp_server._PREPARES_SINCE_WRITE = 0
        self._fake_sb = _FakeSandbox()
        self._orig_sandbox = mcp_server._sandbox
        self._orig_ask = mcp_server._ask_payload
        mcp_server._sandbox = lambda: self._fake_sb
        mcp_server._ask_payload = _fake_ask_payload

    def tearDown(self):
        mcp_server._sandbox = self._orig_sandbox
        mcp_server._ask_payload = self._orig_ask
        mcp_server._PREPARES_SINCE_WRITE = 0

    def _prepare(self, query="门禁链路测试问题"):
        result = mcp_server.call_tool("memory_prepare", {"query": query})
        self.assertFalse(result["isError"])
        return json.loads(_result_text(result))

    def test_prepare_payload_carries_gate_field(self):
        first = self._prepare()
        self.assertEqual(first["sop_gate"], "")
        second = self._prepare()
        self.assertIn("落库门禁", second["sop_gate"])
        self.assertIn("落库门禁", second["hint"])

    def test_remember_clears_gate(self):
        self._prepare()
        result = mcp_server.call_tool(
            "memory_remember", {"question": "门禁测试Q", "answer": "门禁测试A"}
        )
        self.assertFalse(result["isError"])
        self.assertEqual(len(self._fake_sb.remembered), 1)
        again = self._prepare()
        self.assertEqual(again["sop_gate"], "")

    def test_failed_remember_keeps_gate(self):
        self._prepare()
        result = mcp_server.call_tool("memory_remember", {"question": "", "answer": ""})
        self.assertTrue(result["isError"])
        warned = self._prepare()
        self.assertIn("落库门禁", warned["sop_gate"])

    def test_update_success_clears_gate(self):
        self._prepare()
        result = mcp_server.call_tool(
            "memory_update", {"memory_id": "faketest", "answer": "新说法"}
        )
        self.assertFalse(result["isError"])
        self.assertEqual(len(self._fake_sb.updated), 1)
        self.assertEqual(self._prepare()["sop_gate"], "")

    def test_delete_success_clears_gate(self):
        self._prepare()
        result = mcp_server.call_tool(
            "memory_delete", {"memory_id": "faketest"}
        )
        self.assertFalse(result["isError"])
        self.assertEqual(len(self._fake_sb.deleted), 1)
        self.assertEqual(self._prepare()["sop_gate"], "")


if __name__ == "__main__":
    unittest.main()
