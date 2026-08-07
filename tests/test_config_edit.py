"""core/config_edit.py：界面上看/改 config.yaml。

重点全在两件事：密钥不能露出去、也不能被一串星号覆盖掉；文件不能被改坏。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config_edit  # noqa: E402

SAMPLE = """\
feishu:
  enabled: true
  app_id: cli_abc
  app_secret: real-secret-value
  user_access_token: u-real-token
  user_token_expires_at: 1786000000
  refresh_token: ''
  doc_bot_enabled: false
llm:
  provider: cursor
  api_key: crsr_realkey       # 行尾注释
  model: claude-opus-5-thinking-high
"""


class MaskTests(unittest.TestCase):
    def test_secrets_are_hidden_and_the_rest_is_untouched(self):
        masked, keys = config_edit.mask_secrets(SAMPLE)
        self.assertNotIn("real-secret-value", masked)
        self.assertNotIn("u-real-token", masked)
        self.assertNotIn("crsr_realkey", masked)
        self.assertIn("app_id: cli_abc", masked)
        self.assertIn("model: claude-opus-5-thinking-high", masked)
        self.assertEqual(
            sorted(keys),
            ["feishu.app_secret", "feishu.user_access_token", "llm.api_key"],
        )

    def test_a_timestamp_named_token_is_not_a_secret(self):
        """键名里带 token 不代表是密钥：遮掉过期时间只会让人看不到何时要重新授权。"""
        masked, keys = config_edit.mask_secrets(SAMPLE)
        self.assertIn("user_token_expires_at: 1786000000", masked)
        self.assertNotIn("feishu.user_token_expires_at", keys)

    def test_empty_secret_is_left_alone(self):
        """空值遮起来只会让人以为「已经填过了」。"""
        masked, keys = config_edit.mask_secrets(SAMPLE)
        self.assertIn("refresh_token: ''", masked)
        self.assertNotIn("feishu.refresh_token", keys)

    def test_trailing_comment_survives(self):
        masked, _ = config_edit.mask_secrets(SAMPLE)
        self.assertIn(f"api_key: '{config_edit.MASK}'       # 行尾注释", masked)

    def test_masked_text_is_still_valid_yaml(self):
        """掩码要带引号：裸的 ******** 在 YAML 里是别名语法，整份配置会解析失败。"""
        import yaml

        masked, _ = config_edit.mask_secrets(SAMPLE)
        data = yaml.safe_load(masked)
        self.assertEqual(data["feishu"]["app_id"], "cli_abc")
        self.assertEqual(data["feishu"]["app_secret"], config_edit.MASK)


class RestoreTests(unittest.TestCase):
    def test_untouched_mask_becomes_the_original_value(self):
        masked, _ = config_edit.mask_secrets(SAMPLE)
        restored, unresolved = config_edit.restore_secrets(masked, SAMPLE)
        self.assertEqual(unresolved, [])
        self.assertEqual(restored, SAMPLE)

    def test_a_typed_in_secret_wins_over_the_original(self):
        masked, _ = config_edit.mask_secrets(SAMPLE)
        edited = masked.replace(f"app_secret: '{config_edit.MASK}'", "app_secret: brand-new")
        restored, unresolved = config_edit.restore_secrets(edited, SAMPLE)
        self.assertEqual(unresolved, [])
        self.assertIn("app_secret: brand-new", restored)
        # 其它密钥仍然还原成原值
        self.assertIn("user_access_token: u-real-token", restored)

    def test_mask_on_a_field_that_never_had_a_value_is_refused(self):
        text = SAMPLE.replace("refresh_token: ''", f"refresh_token: '{config_edit.MASK}'")
        _restored, unresolved = config_edit.restore_secrets(text, SAMPLE)
        self.assertEqual(unresolved, ["feishu.refresh_token"])

    def test_edits_to_normal_fields_are_kept(self):
        masked, _ = config_edit.mask_secrets(SAMPLE)
        edited = masked.replace("doc_bot_enabled: false", "doc_bot_enabled: true")
        restored, _ = config_edit.restore_secrets(edited, SAMPLE)
        self.assertIn("doc_bot_enabled: true", restored)
        self.assertIn("app_secret: real-secret-value", restored)


class ValidateTests(unittest.TestCase):
    def test_good_config_passes(self):
        self.assertEqual(config_edit.validate(SAMPLE), "")

    def test_broken_yaml_is_rejected(self):
        err = config_edit.validate("feishu:\n  app_id: [unclosed\n")
        self.assertIn("YAML", err)

    def test_empty_is_rejected(self):
        self.assertIn("空", config_edit.validate("\n\n"))

    def test_scalar_top_level_is_rejected(self):
        self.assertIn("键值对", config_edit.validate("就一句话\n"))

    def test_a_section_written_as_a_scalar_is_caught(self):
        """YAML 合法不代表能当配置用：小节写成字符串要在保存前就拦下。"""
        self.assertNotEqual(config_edit.validate("feishu: 就一个字符串\n"), "")

    def test_scalar_type_mistakes_are_not_caught(self):
        """
        说清楚校验的边界：load_config 不校验标量类型，top_k 写成中文也照收，
        要到真用的时候才炸。别指望这个函数能挡住所有错。
        """
        self.assertEqual(config_edit.validate("long_term:\n  top_k: 不是数字\n"), "")


class SaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.yaml"
        self.path.write_text(SAMPLE, encoding="utf-8")
        self._saved = config_edit.config_path
        config_edit.config_path = lambda: self.path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        config_edit.config_path = self._saved
        self.tmp.cleanup()

    def test_read_view_hides_secrets(self):
        view = config_edit.read_view()
        self.assertTrue(view.exists)
        self.assertNotIn("real-secret-value", view.text)
        self.assertIn("feishu.app_secret", view.masked)

    def test_round_trip_keeps_the_file_byte_identical(self):
        """读出来不改直接存，文件必须一个字节都不变——尤其是 token。"""
        view = config_edit.read_view()
        res = config_edit.save(view.text)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)

    def test_saving_an_edit_keeps_the_secrets(self):
        view = config_edit.read_view()
        res = config_edit.save(view.text.replace("doc_bot_enabled: false", "doc_bot_enabled: true"))
        self.assertTrue(res.ok, res.error)
        after = self.path.read_text(encoding="utf-8")
        self.assertIn("doc_bot_enabled: true", after)
        self.assertIn("app_secret: real-secret-value", after)
        self.assertIn("api_key: crsr_realkey", after)
        self.assertTrue(res.backup, "改动前应该留一份备份")
        self.assertEqual(Path(res.backup).read_text(encoding="utf-8"), SAMPLE)

    def test_broken_input_leaves_the_file_alone(self):
        res = config_edit.save("feishu:\n  app_id: [unclosed\n")
        self.assertFalse(res.ok)
        self.assertIn("YAML", res.error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)

    def test_mask_on_an_empty_field_is_refused_without_writing(self):
        view = config_edit.read_view()
        bad = view.text.replace("refresh_token: ''", f"refresh_token: '{config_edit.MASK}'")
        res = config_edit.save(bad)
        self.assertFalse(res.ok)
        self.assertIn("refresh_token", res.error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)

    def test_empty_text_is_refused(self):
        res = config_edit.save("   ")
        self.assertFalse(res.ok)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)

    def test_no_change_reports_but_does_not_rewrite(self):
        view = config_edit.read_view()
        res = config_edit.save(view.text)
        self.assertTrue(res.ok)
        again = config_edit.save(view.text)
        self.assertTrue(again.ok)
        self.assertIn("没有变化", again.message)


if __name__ == "__main__":
    unittest.main()
