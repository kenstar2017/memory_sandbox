#!/usr/bin/env python3
"""核心链路冒烟测试（标准库 unittest）。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import AppConfig, LLMConfig, LongTermConfig, SensoryConfig, WorkingConfig
from core.utils import assemble_long_term_query


class MemorySandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_sandbox_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(
                persist_dir=self.tmp,
                similarity_threshold=0.65,
                top_k=3,
            ),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sensory_reject_empty(self):
        r = self.sb.chat("   ")
        self.assertEqual(r.source, "sensory_reject")

    def test_working_arith_and_followup(self):
        r1 = self.sb.chat("计算 3*4")
        self.assertEqual(r1.source, "working")
        self.assertEqual(r1.answer, "12")
        r2 = self.sb.chat("再加3")
        self.assertEqual(r2.source, "working")
        self.assertEqual(r2.answer, "15")

    def test_long_term_hit_skips_llm(self):
        self.sb.remember("构建命令", "pnpm build")
        r = self.sb.chat("构建命令是什么")
        self.assertEqual(r.source, "long_term")
        self.assertIn("pnpm build", r.answer)

    def test_llm_fallback(self):
        r = self.sb.chat("一个从未见过的冷门问题 xyzzy-42")
        self.assertEqual(r.source, "llm")
        self.assertIn("MockLLM", r.answer)

    def test_forget_command(self):
        self.sb.remember("密钥口令", "不要外传")
        r = self.sb.chat("忘记：密钥")
        self.assertEqual(r.source, "command")
        r2 = self.sb.chat("密钥口令是什么")
        self.assertEqual(r2.source, "llm")

    def test_clear_long_term_requires_confirm(self):
        self.sb.remember("重要知识", "不要丢")
        r = self.sb.chat("清空长时记忆")
        self.assertEqual(r.source, "command")
        self.assertTrue(r.meta.get("needs_confirm"))
        self.assertEqual(len(self.sb.long_term.records), 1)
        r2 = self.sb.chat("确认清空长时记忆")
        self.assertEqual(r2.source, "command")
        self.assertIn("已清空", r2.answer)
        self.assertEqual(len(self.sb.long_term.records), 0)

    def test_long_term_list_is_newest_first(self):
        """侧栏是「最近」式列表，新记忆要在最上面。"""
        self.sb.remember("问题甲", "答一")
        self.sb.remember("问题乙", "答二")
        titles = [r["question"] for r in self.sb.list_long_term()["declarative"]]
        self.assertEqual(titles[:2], ["问题乙", "问题甲"])

    def test_long_term_limit_drops_oldest_not_newest(self):
        """原来 records[:limit] 截掉的正好是最新那批。"""
        for i in range(5):
            self.sb.remember(f"问题{i}", f"答案{i}")
        data = self.sb.list_long_term(limit=2)
        titles = [r["question"] for r in data["declarative"]]
        self.assertEqual(titles, ["问题4", "问题3"])
        self.assertEqual(data["declarative_total"], 5)

    def test_limit_zero_returns_everything(self):
        """列表界面要全量：截断过的列表里，最老那批在界面上根本点不开。"""
        for i in range(5):
            self.sb.remember(f"问题{i}", f"答案{i}")
        data = self.sb.list_long_term(limit=0)
        self.assertEqual(len(data["declarative"]), 5)
        self.assertEqual(data["declarative_total"], 5)

    def test_truncated_text_view_says_so(self):
        """表头写总数、底下只列一部分，不吭声就会被当成记忆丢了。"""
        for i in range(5):
            self.sb.remember(f"问题{i}", f"答案{i}")
        original = self.sb.list_long_term
        self.sb.list_long_term = lambda limit=2: original(limit=2)  # type: ignore[assignment]
        try:
            text = self.sb.format_memory_view("long_term")
        finally:
            self.sb.list_long_term = original  # type: ignore[assignment]
        self.assertIn("还有 3 条", text)

    def _content_fields(self, rec: dict) -> tuple:
        """BloomBox 算内容指纹用的那几个字段（desktop/src/App.tsx contentStamp）。"""
        return (
            rec["question"],
            rec["answer"],
            rec["scene"],
            rec["kind"],
            tuple(rec["tags"]),
        )

    def test_retrieval_does_not_touch_the_fields_bloombox_fingerprints(self):
        """
        检索命中会 reinforce（改 hit_count / weight / updated_at）并重写文件。

        BloomBox 靠内容指纹判断「这条被别处改写过」，指纹里绝不能掺这些字段：
        掺了的话，别的 agent 查一次记忆，侧栏就会冒出一片「N 条改」。
        """
        self.sb.remember("画板怎么读", "开通 board:whiteboard:node:read 后调节点接口")
        before = self.sb.list_long_term()["declarative"][0]
        self.sb.chat("画板怎么读")
        after = self.sb.list_long_term()["declarative"][0]
        self.assertEqual(self._content_fields(before), self._content_fields(after))

    def test_in_place_update_changes_the_fingerprint(self):
        """条数不变的原地覆盖，必须能从内容字段上看出来。"""
        self.sb.remember("画板怎么读", "旧结论：读不到")
        before = self.sb.list_long_term()["declarative"][0]
        self.sb.update_memory(memory_id=before["id"], answer="新结论：已接入画板接口")
        after = self.sb.list_long_term()["declarative"][0]
        self.assertEqual(before["id"], after["id"])
        self.assertEqual(len(self.sb.list_long_term()["declarative"]), 1)
        self.assertNotEqual(self._content_fields(before), self._content_fields(after))

    def _only_record(self):
        return self.sb.long_term.records[0]

    def test_update_replaces_answer_in_place(self):
        """过时结论要被改掉，而不是并存两条打架的说法。"""
        self.sb.remember("z-index 号段怎么分", "agent 1110、overlay-high 999")
        rid = self._only_record().id
        msg = self.sb.update_memory(
            memory_id=rid, answer="迁移后：agent 1012、SideSheet 走文档流排序"
        )
        self.assertIn(rid, msg)
        self.assertEqual(len(self.sb.long_term.records), 1)
        rec = self._only_record()
        self.assertIn("1012", rec.answer)
        self.assertNotIn("1110", rec.answer)

    def test_update_keeps_question_tags_kind_by_default(self):
        self.sb.remember(
            "z-index 号段怎么分", "旧值", tags=["frontend"], kind="decision"
        )
        rec = self._only_record()
        question, tags, kind = rec.question, list(rec.tags), rec.kind
        self.sb.update_memory(memory_id=rec.id, answer="新值")
        after = self._only_record()
        self.assertEqual(after.question, question)
        self.assertEqual(sorted(after.tags), sorted(tags))
        self.assertEqual(after.kind, kind)

    def test_update_can_change_question(self):
        self.sb.remember("旧的问法是什么", "答")
        rid = self._only_record().id
        self.sb.update_memory(
            memory_id=rid, answer="答", new_question="新的问法是什么"
        )
        self.assertEqual(len(self.sb.long_term.records), 1)
        self.assertIn("新的问法", self._only_record().question)

    def test_update_unknown_id_reports_instead_of_creating(self):
        """定位不到就必须报错：静默新建会留下重复且互相矛盾的记录。"""
        self.sb.remember("已有的问", "答")
        msg = self.sb.update_memory(memory_id="deadbeef1234", answer="新答")
        self.assertIn("未找到", msg)
        self.assertEqual(len(self.sb.long_term.records), 1)

    def test_update_rejects_empty_answer(self):
        self.sb.remember("已有的问", "答")
        msg = self.sb.update_memory(memory_id=self._only_record().id, answer="   ")
        self.assertIn("不能为空", msg)
        self.assertEqual(self._only_record().answer, "答")

    def test_update_by_question_when_no_id(self):
        self.sb.remember("按问法定位的条目", "旧答")
        rec = self._only_record()
        msg = self.sb.update_memory(question=rec.question, answer="新答")
        self.assertIn(rec.id, msg)
        self.assertEqual(self._only_record().answer, "新答")

    def test_updated_memory_is_what_retrieval_returns(self):
        """改完之后检索必须给新值，否则等于没改。"""
        self.sb.remember("z-index 号段怎么分", "agent 1110")
        self.sb.update_memory(
            memory_id=self._only_record().id, answer="agent 1012"
        )
        pack = self.sb.build_reference_pack("z-index 号段怎么分")
        self.assertIn("1012", pack["context_pack"])
        self.assertNotIn("1110", pack["context_pack"])

    def test_context_pack_exposes_ids(self):
        """hook 投递的是纯文本，没有 id 的话 agent 无法回头修这条。"""
        self.sb.remember("z-index 号段怎么分", "取值表")
        rid = self._only_record().id
        pack = self.sb.build_reference_pack("z-index 号段怎么分")
        self.assertIn(f"id={rid}", pack["context_pack"])

    def test_context_pack_tells_agent_how_to_fix(self):
        self.sb.remember("z-index 号段怎么分", "取值表")
        pack = self.sb.build_reference_pack("z-index 号段怎么分")
        self.assertIn("memory_update", pack["context_pack"])

    def test_reference_pack_has_no_side_effects(self):
        """
        每条用户消息都会走一次预取（beforeSubmitPrompt hook），
        所以这条路必须只读：一旦 reinforce 就会把权重和命中数刷爆，
        还会改动记忆文件、连带触发 BloomBox 的「有新写入」提示。
        """
        self.sb.remember("zIndex 怎么治理", "用统一常量表")
        before = self.sb.list_long_term()["declarative"][0]
        revision = self.sb.long_term.revision()

        pack = self.sb.build_reference_pack("zIndex 怎么治理")
        self.assertTrue(pack["context_pack"])

        after = self.sb.list_long_term()["declarative"][0]
        self.assertEqual(after["hit_count"], before["hit_count"])
        self.assertEqual(after["weight"], before["weight"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(self.sb.long_term.revision(), revision)

    def test_reference_pack_empty_when_nothing_relevant(self):
        """空包是「本轮不用拦」的依据，必须真的为空而不是一段说明文字。"""
        self.sb.remember("zIndex 怎么治理", "用统一常量表")
        pack = self.sb.build_reference_pack("量子色动力学的耦合常数")
        self.assertEqual(pack["context_pack"], "")

    def test_revision_changes_after_write(self):
        """前端靠这个标记轮询「别处有没有写入」。"""
        before = self.sb.long_term.revision()
        self.sb.remember("新写入的问", "答")
        self.assertNotEqual(self.sb.long_term.revision(), before)

    def test_revision_stable_without_write(self):
        first = self.sb.long_term.revision()
        self.assertEqual(self.sb.long_term.revision(), first)

    def test_revision_sees_other_process_write(self):
        """MCP / CLI 是另一个进程，改的是同一份 declarative.json。"""
        self.sb.remember("甲", "答一")
        before = self.sb.long_term.revision()
        other = MemorySandbox(config=self.cfg)
        other.remember("乙", "答二")
        self.assertNotEqual(self.sb.long_term.revision(), before)

    def test_long_term_list_exposes_timestamps(self):
        self.sb.remember("带时间的问", "答")
        rec = self.sb.list_long_term()["declarative"][0]
        self.assertGreater(rec["updated_at"], 0)
        self.assertGreater(rec["created_at"], 0)

    def test_retrieval_does_not_reorder_list(self):
        """命中会 reinforce updated_at；若按它排序，别处查一次记忆就重排列表。"""
        self.sb.remember("问题甲", "答一")
        self.sb.remember("问题乙", "答二")
        self.sb.chat("问题甲是什么")
        titles = [r["question"] for r in self.sb.list_long_term()["declarative"]]
        self.assertEqual(titles[:2], ["问题乙", "问题甲"])

    def test_backup_and_restore_long_term(self):
        self.sb.remember("备份测试问", "备份测试答")
        msg = self.sb.backup_long_term()
        self.assertIn("已备份", msg)
        backups = self.sb.long_term.list_backups()
        self.assertTrue(backups)
        self.sb.long_term.forget()
        self.assertEqual(len(self.sb.long_term.records), 0)
        restored = self.sb.restore_long_term(str(backups[0]))
        self.assertIn("恢复", restored)
        self.assertEqual(len(self.sb.long_term.records), 1)
        self.assertEqual(self.sb.long_term.records[0].answer, "备份测试答")

    def test_assemble_long_term_query(self):
        self.assertEqual(
            assemble_long_term_query("revenue 怎么本地启动"),
            "revenue 怎么本地启动，记录到长期记忆。",
        )
        self.assertEqual(
            assemble_long_term_query("revenue 怎么本地启动，记录到长期记忆。"),
            "revenue 怎么本地启动，记录到长期记忆。",
        )

    def test_tags_remember_and_filter(self):
        self.sb.remember("飞书登录", "用 oauth 脚本", scene="dev", tags=["feishu", "oauth"])
        self.sb.remember("前端构建", "pnpm build", scene="dev", tags=["frontend"])
        hits = self.sb.long_term.search_hits("登录", tags=["feishu"])
        self.assertTrue(hits)
        self.assertIn("feishu", hits[0].record.tags)
        self.assertTrue(any("tag:" in r for r in hits[0].reasons))
        front = self.sb.long_term.search_hits("构建", tags=["frontend"])
        self.assertTrue(front)
        self.assertEqual(front[0].record.answer, "pnpm build")
        miss = self.sb.long_term.search_hits("登录", tags=["frontend"], threshold=0.01)
        self.assertFalse(any("oauth" in h.record.answer for h in miss))

    def test_hash_tag_in_question(self):
        self.sb.remember("如何配置 #feishu 机器人", "见 docs/feishu_zh.md", scene="dev")
        self.assertIn("feishu", self.sb.long_term.records[0].tags)

    def test_explainable_long_term_hit(self):
        self.sb.remember("mock 端口", "3001", scene="dev", tags=["dev"])
        r = self.sb.ask_local("mock 端口是多少")
        self.assertEqual(r.source, "long_term")
        self.assertIn("3001", r.answer)
        self.assertTrue(r.meta.get("hits"))
        self.assertIn("score", r.meta["hits"][0])
        self.assertTrue(r.meta["hits"][0].get("reasons") or r.meta.get("explain"))

    def test_concurrent_remember_no_lost_update(self):
        import threading

        errors = []

        def writer(i):
            try:
                other = MemorySandbox(config=self.cfg)
                other.remember(f"并发键-{i}", f"值-{i}", scene="dev", tags=["race"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.sb.long_term.reload()
        race = [r for r in self.sb.long_term.records if "race" in (r.tags or [])]
        self.assertGreaterEqual(len(race), 8)


class NaturalLanguageRememberTests(unittest.TestCase):
    """在 BloomBox / CLI 里说人话就能写库，不必背「记住：」的格式。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_sandbox_nl_")
        self.cfg = AppConfig(
            long_term=LongTermConfig(persist_dir=self.tmp),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _questions(self):
        return [r.question for r in self.sb.long_term.records]

    def test_various_phrasings_all_write(self):
        for text in (
            "记一下：mock 端口是 3001",
            "帮我记住 构建命令是 pnpm build",
            "把 灰度开关在 config.yaml 记一下",
            "本地 bot 日志在 /tmp/bot_run.log，记下来",
        ):
            r = self.sb.chat(text)
            self.assertEqual(r.source, "command", text)
        self.assertEqual(len(self.sb.long_term.records), 4)

    def test_arrow_still_splits_question_and_answer(self):
        self.sb.chat("记住：长连接怎么保存 => 先跑本地进程再点保存")
        record = self.sb.long_term.records[0]
        self.assertEqual(record.question, "长连接怎么保存")
        self.assertEqual(record.answer, "先跑本地进程再点保存")

    def test_deictic_grabs_the_last_exchange(self):
        # 「这个结论」指的是刚才那轮对话，不是字面上的四个字
        self.sb.working.add_context("这次报警的根因是什么", role="user")
        self.sb.working.add_context("邀约签约开始日期过期", role="assistant")
        r = self.sb.chat("记一下这个结论")
        self.assertEqual(r.source, "command")
        record = self.sb.long_term.records[-1]
        self.assertIn("报警", record.question)
        self.assertEqual(record.answer, "邀约签约开始日期过期")

    def test_deictic_without_any_context_asks_for_content(self):
        r = self.sb.chat("记一下这个结论")
        self.assertEqual(r.source, "command")
        self.assertIn("要记什么", r.answer)
        self.assertEqual(self.sb.long_term.records, [])

    def test_fixed_commands_win_over_the_detector(self):
        # 「备份长时记忆」是指令，不是要记的内容
        r = self.sb.chat("备份长时记忆")
        self.assertEqual(r.source, "command")
        self.assertEqual(self._questions(), [])

    def test_questions_are_not_written(self):
        for text in ("保存位置在哪", "记忆库怎么用"):
            self.sb.chat(text)
        self.assertEqual(self._questions(), [])

    def test_mcp_path_never_writes(self):
        # memory_ask 对调用方是只读的，检索一句话不该顺手改库
        r = self.sb.ask_local("记一下：mock 端口是 3001", allow_commands=False)
        self.assertNotEqual(r.source, "command")
        self.assertEqual(self._questions(), [])

    def test_prepare_suffix_is_a_query_not_a_write(self):
        assembled = assemble_long_term_query("飞书机器人怎么配权限")
        self.sb.ask_local(assembled)
        self.assertEqual(self._questions(), [])


if __name__ == "__main__":
    unittest.main()
