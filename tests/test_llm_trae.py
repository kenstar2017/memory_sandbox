"""Trae（TraeCode CLI）回退 provider。

重点是**命令行拼装**：traecli 和 Cursor agent CLI 的参数不通用，尤其 `-p` 在 traecli 里是
`--profile` 而不是 prompt。照 Claude Code 的写法传 `-p "问题"` 不会报错，只会把问题当 profile
名，然后拿一个莫名其妙的结果回来——所以这里把这条当回归用例钉住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import LLMConfig  # noqa: E402
from core.llm import (  # noqa: E402
    TraeLocalAgentLLM,
    build_llm,
    build_local_agent_prompt,
    build_trae_cmd,
    describe_llm_target,
    resolve_trae_bin,
)


class BuildTraeCmdTests(unittest.TestCase):
    def test_uses_exec_subcommand_and_cd(self):
        cmd = build_trae_cmd("/bin/coco", "问题", "/tmp", "ask")
        self.assertEqual(cmd[0], "/bin/coco")
        self.assertEqual(cmd[1], "exec")
        self.assertIn("-C", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], "/tmp")
        # traecli 没有 --workspace（那是 cursor-agent 的）
        self.assertNotIn("--workspace", cmd)

    def test_prompt_is_positional_never_behind_dash_p(self):
        cmd = build_trae_cmd("/bin/coco", "问题", "/tmp", "ask")
        self.assertEqual(cmd[-1], "问题")
        # -p 是 --profile；prompt 绝不能挂在它后面
        self.assertNotIn("-p", cmd)
        self.assertNotIn("--profile", cmd)

    def test_agent_mode_maps_to_sandbox_policy(self):
        for mode in ("ask", "plan", "ASK"):
            cmd = build_trae_cmd("/bin/coco", "q", "/tmp", mode)
            self.assertEqual(cmd[cmd.index("-s") + 1], "read-only", mode)
        for mode in ("", "agent", "full"):
            cmd = build_trae_cmd("/bin/coco", "q", "/tmp", mode)
            self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write", mode)

    def test_optional_model_and_output_file(self):
        bare = build_trae_cmd("/bin/coco", "q", "/tmp", "ask")
        self.assertNotIn("-m", bare)
        self.assertNotIn("-o", bare)

        full = build_trae_cmd(
            "/bin/coco", "q", "/tmp", "ask", model="doubao", last_message_file="/tmp/a.txt"
        )
        self.assertEqual(full[full.index("-m") + 1], "doubao")
        self.assertEqual(full[full.index("-o") + 1], "/tmp/a.txt")

    def test_skips_git_repo_check(self):
        # 回退问答的 cwd 常是 ~/Documents 这类非 git 目录
        self.assertIn("--skip-git-repo-check", build_trae_cmd("/bin/coco", "q", "/tmp", "ask"))


class ProviderWiringTests(unittest.TestCase):
    def test_build_llm_returns_trae_provider(self):
        for name in ("trae", "trae_cli", "coco", "TRAE"):
            llm = build_llm(LLMConfig(enabled=True, provider=name))
            self.assertIsInstance(llm, TraeLocalAgentLLM, name)

    def test_disabled_llm_still_returns_none(self):
        self.assertIsNone(build_llm(LLMConfig(enabled=False, provider="trae")))

    def test_explicit_trae_bin_wins(self):
        self.assertEqual(resolve_trae_bin(LLMConfig(trae_bin="/opt/x/coco")), "/opt/x/coco")

    def test_describe_dispatches_by_provider(self):
        self.assertTrue(
            describe_llm_target(LLMConfig(provider="trae", cwd="/tmp")).startswith("trae/local")
        )
        self.assertTrue(
            describe_llm_target(LLMConfig(provider="cursor", cwd="/tmp")).startswith("cursor/")
        )
        self.assertEqual(describe_llm_target(LLMConfig(provider="mock")), "provider=mock")

    def test_missing_binary_reports_instead_of_raising(self):
        # 构造期与 generate 都会解析 bin，且 generate 解析失败时会保留构造期的结果，
        # 所以打桩必须覆盖两处；否则本机装了 coco 时这条用例会真的去调模型。
        with mock.patch("core.llm.resolve_trae_bin", return_value=None):
            llm = TraeLocalAgentLLM(LLMConfig(provider="trae"))
            answer = llm.generate("问题")
        self.assertIn("[LLM Error]", answer)
        self.assertIn("未找到本机 Trae CLI", answer)

    def test_bogus_binary_reports_instead_of_raising(self):
        llm = TraeLocalAgentLLM(
            LLMConfig(provider="trae", trae_bin="/nonexistent/coco", cwd=str(ROOT))
        )
        self.assertIn("[LLM Error]", llm.generate("问题"))


class SharedPromptTests(unittest.TestCase):
    def test_read_only_modes_forbid_edits(self):
        text = build_local_agent_prompt("ask", "问题")
        self.assertIn("只读", text)
        self.assertIn("问题", text)

    def test_agent_mode_allows_edits(self):
        self.assertIn("全工具", build_local_agent_prompt("", "问题"))

    def test_context_is_injected(self):
        text = build_local_agent_prompt("ask", "问题", "上下文内容")
        self.assertIn("上下文内容", text)

    def test_git_push_ban_present_in_both_modes(self):
        for mode in ("ask", ""):
            self.assertIn("git push", build_local_agent_prompt(mode, "问题"))


if __name__ == "__main__":
    unittest.main()
