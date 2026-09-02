"""界面上切换回退接入方（Cursor / Trae / 离线占位）。

两个容易出事的点：
1. 切到缺配置的 provider（OpenAI 网关没填 base_url）会在构建时抛，不能把沙箱留在
   没有 LLM 的半残状态——必须原样退回旧 provider。
2. 写盘是**合并**写 llm.*，不能把用户已有的 agent_mode 等字段冲掉。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import MemorySandbox  # noqa: E402
from core.config import (  # noqa: E402
    AppConfig,
    LLMConfig,
    LongTermConfig,
    persist_llm_provider,
)
from core.llm import (  # noqa: E402
    MockLLM,
    TraeLocalAgentLLM,
    llm_provider_options,
    normalize_llm_provider,
    provider_label,
)


class NormalizeProviderTests(unittest.TestCase):
    def test_aliases_collapse_to_canonical_names(self):
        for alias in ("trae", "coco", "traecli", "TRAE", " trae_cli "):
            self.assertEqual(normalize_llm_provider(alias), "trae", alias)
        for alias in ("cursor", "cursor-agent", "CURSOR"):
            self.assertEqual(normalize_llm_provider(alias), "cursor", alias)
        self.assertEqual(normalize_llm_provider("cursor_cloud"), "cursor_cloud")
        self.assertEqual(normalize_llm_provider("openai"), "openai_compatible")

    def test_unknown_and_empty_are_rejected_up_front(self):
        # 留到真正回退那一刻才报错，用户已经等了半天了
        for bad in ("", "   ", "gpt5", "claude"):
            with self.assertRaises(ValueError):
                normalize_llm_provider(bad)

    def test_label_falls_back_to_raw_value(self):
        self.assertEqual(provider_label("trae"), "Trae CLI")
        self.assertEqual(provider_label("不认识的"), "不认识的")


class ProviderOptionsTests(unittest.TestCase):
    def test_default_options_cover_the_switchable_ones(self):
        values = [o["value"] for o in llm_provider_options("cursor")]
        self.assertEqual(values, ["cursor", "trae", "mock"])

    def test_current_provider_is_always_present(self):
        # 配了 OpenAI 网关时，下拉不能显示成 cursor，否则像是已经切走了
        values = [o["value"] for o in llm_provider_options("openai_compatible")]
        self.assertIn("openai_compatible", values)

    def test_unknown_current_does_not_break_options(self):
        self.assertEqual(
            [o["value"] for o in llm_provider_options("乱填的")],
            ["cursor", "trae", "mock"],
        )


class SetLlmProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_sandbox_provider_")
        self.sb = MemorySandbox(
            config=AppConfig(
                long_term=LongTermConfig(persist_dir=self.tmp),
                llm=LLMConfig(enabled=True, provider="mock"),
            )
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_switch_rebuilds_the_llm_instance(self):
        msg = self.sb.set_llm_provider("trae", persist=False)
        self.assertEqual(self.sb.config.llm.provider, "trae")
        self.assertIsInstance(self.sb.llm, TraeLocalAgentLLM)
        self.assertIn("Trae", msg)

    def test_switch_accepts_aliases(self):
        self.sb.set_llm_provider("coco", persist=False)
        self.assertEqual(self.sb.config.llm.provider, "trae")

    def test_failed_switch_reverts_to_previous_provider(self):
        # openai_compatible 没 base_url 会在构建时抛
        with self.assertRaises(ValueError):
            self.sb.set_llm_provider("openai_compatible", persist=False)
        self.assertEqual(self.sb.config.llm.provider, "mock")
        self.assertIsInstance(self.sb.llm, MockLLM)

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            self.sb.set_llm_provider("gpt5", persist=False)
        self.assertEqual(self.sb.config.llm.provider, "mock")

    def test_warns_when_llm_is_disabled(self):
        self.sb.config.llm.enabled = False
        msg = self.sb.set_llm_provider("trae", persist=False)
        self.assertIn("enabled=false", msg)
        # enabled=false 时 build_llm 返回 None，这不算切换失败
        self.assertIsNone(self.sb.llm)

    def test_switching_clears_the_model_of_the_previous_provider(self):
        # 模型名两边不通用，留着上一家的会等到真正回退那一刻才炸：
        # Trae 报 thread/start -32603，Cursor 报 Cannot use this model
        self.sb.config.llm.model = "claude-opus-5-thinking-high"
        msg = self.sb.set_llm_provider("trae", persist=False)
        self.assertEqual(self.sb.config.llm.model, "")
        self.assertIn("已清空 llm.model", msg)
        # 新实例必须也拿到空模型，否则命令行里照旧带 --model
        self.assertEqual(self.sb.llm.model, "")

    def test_reselecting_the_same_provider_keeps_the_model(self):
        self.sb.set_llm_provider("trae", persist=False)
        self.sb.config.llm.model = "GPT-5.6-Sol"
        msg = self.sb.set_llm_provider("coco", persist=False)  # 同一个接入方的别名
        self.assertEqual(self.sb.config.llm.model, "GPT-5.6-Sol")
        self.assertNotIn("已清空", msg)

    def test_failed_switch_restores_the_model_too(self):
        self.sb.config.llm.model = "claude-opus-5-thinking-high"
        with self.assertRaises(ValueError):
            self.sb.set_llm_provider("openai_compatible", persist=False)
        self.assertEqual(self.sb.config.llm.provider, "mock")
        self.assertEqual(self.sb.config.llm.model, "claude-opus-5-thinking-high")


class PersistProviderTests(unittest.TestCase):
    def setUp(self):
        # _user_config_path 只认路径里带 Application Support 的用户配置
        self.tmp = tempfile.mkdtemp(prefix="mem_sandbox_cfg_")
        self.cfg_dir = Path(self.tmp) / "Application Support" / "MemorySandbox"
        self.cfg_dir.mkdir(parents=True)
        self.cfg_path = self.cfg_dir / "config.yaml"
        self.cfg_path.write_text(
            yaml.safe_dump({"llm": {"agent_mode": "plan", "timeout": 900}}, allow_unicode=True),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_provider_is_merged_without_clobbering_siblings(self):
        written = persist_llm_provider(str(self.cfg_path), "trae")
        self.assertEqual(written, str(self.cfg_path))
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"]["provider"], "trae")
        self.assertEqual(raw["llm"]["agent_mode"], "plan")
        self.assertEqual(raw["llm"]["timeout"], 900)

    def test_model_is_left_alone_unless_passed(self):
        self.cfg_path.write_text(
            yaml.safe_dump({"llm": {"model": "GPT-5.6-Sol"}}, allow_unicode=True),
            encoding="utf-8",
        )
        persist_llm_provider(str(self.cfg_path), "trae")
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"]["model"], "GPT-5.6-Sol")

    def test_model_is_cleared_when_passed_empty(self):
        # 切接入方时要把上一家的模型名一起清掉，否则下次启动又读回来
        self.cfg_path.write_text(
            yaml.safe_dump({"llm": {"model": "GPT-5.6-Sol"}}, allow_unicode=True),
            encoding="utf-8",
        )
        persist_llm_provider(str(self.cfg_path), "cursor", model="")
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"]["provider"], "cursor")
        self.assertEqual(raw["llm"]["model"], "")


if __name__ == "__main__":
    unittest.main()
