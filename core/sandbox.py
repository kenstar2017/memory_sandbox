"""记忆沙箱主编排：感觉 → 工作 → 长时 →（可选）大模型 → 回写。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .config import AppConfig, load_config
from .embedding import LocalHasherEmbedder
from .llm import BaseLLM, ProgressCallback, build_llm
from .long_term import LongTermMemory
from .paths import default_config_path, default_persist_dir, is_frozen, resource_root
from .sensory import SensoryMemory
from .extract import extract_memory_candidates, suggest_tags_from_text
from .tags import merge_tags, parse_tags_from_text
from .utils import extract_keywords
from .working import WorkingMemory

_PROJECT_ROOT = resource_root()


@dataclass
class ChatResult:
    answer: str
    source: str  # sensory_reject | working | long_term | procedural | llm | command | miss
    meta: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.answer


class MemorySandbox:
    """
    统一入口 chat(input_text)：
    1. 感觉记忆预处理
    2. 工作记忆本地匹配
    3. 长时记忆检索
    4. 沙箱无解 → 大模型
    5. 结果回写巩固
    """

    def __init__(self, config: Optional[AppConfig] = None, config_path: Optional[str] = None):
        cfg_path = config_path or str(default_config_path())
        self.config_path = cfg_path
        self.config = config or load_config(cfg_path)
        self.embedder = LocalHasherEmbedder(dim=self.config.embedding.dim)

        self.sensory = SensoryMemory(ttl=self.config.sensory.ttl)
        self.working = WorkingMemory(
            chunk_size=self.config.working.chunk_size,
            idle_clear_seconds=self.config.working.idle_clear_seconds,
        )
        self.working.scene = self.config.sandbox.default_scene

        persist_dir = Path(self.config.long_term.persist_dir)
        if not persist_dir.is_absolute():
            # 打包态默认写到 Application Support；开发态写到项目 data/
            persist_dir = (default_persist_dir() if is_frozen() else _PROJECT_ROOT / persist_dir)

        lt = self.config.long_term
        self.long_term = LongTermMemory(
            persist_dir=str(persist_dir),
            similarity_threshold=lt.similarity_threshold,
            top_k=lt.top_k,
            reinforce_boost=lt.reinforce_boost,
            embedder=self.embedder,
            bm25_enabled=getattr(lt, "bm25_enabled", True),
            vector_weight=getattr(lt, "vector_weight", 0.55),
            keyword_weight=getattr(lt, "keyword_weight", 0.20),
            bm25_weight=getattr(lt, "bm25_weight", 0.25),
            aging_enabled=getattr(lt, "aging_enabled", True),
            aging_days=getattr(lt, "aging_days", 90.0),
            aging_min_hits=getattr(lt, "aging_min_hits", 0),
            aging_decay=getattr(lt, "aging_decay", 0.15),
        )
        self.llm: Optional[BaseLLM] = build_llm(self.config.llm)

    # ---------- public API ----------
    def ask_local(
        self,
        input_text: str,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ChatResult:
        """
        仅检索感觉 / 工作 / 长时（含程序性）记忆，绝不调用沙箱内 LLM。
        供外部 AI 工具（Cursor MCP 等）使用：未命中由外部模型继续推理。
        """
        def _p(msg: str) -> None:
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass

        cmd = self._handle_command(input_text)
        if cmd is not None:
            return cmd

        _p("感觉记忆：预处理输入…")
        item = self.sensory.add(input_text)
        valid = self.sensory.get_valid_data()
        if item is None or not valid:
            return ChatResult(answer="无效输入", source="sensory_reject")

        text = item.text
        keywords = item.keywords
        vec = self.embedder.embed(text)

        # 「重试 / 再试一次」：跳过工作记忆复用，强制重新走检索/飞书/LLM
        force_retry = bool(
            re.search(r"(?:请)?(?:再试一次|重新试|重试|重新分析|重新拉取|再分析一遍)", text)
        )
        _p("工作记忆：本地匹配…")
        local_res = None if force_retry else self.working.local_match(text, user_vec=vec)
        if force_retry:
            _p("检测到重试意图，跳过工作记忆复用")
        if local_res:
            self._write_back(text, keywords, vec, local_res, persist_long=False)
            _p("命中工作记忆")
            return ChatResult(answer=local_res, source="working")

        proc = self.long_term.match_procedure(text)
        if proc and ("模板" in text or text.strip() in self.long_term.procedural):
            self._write_back(text, keywords, vec, proc, persist_long=False)
            _p("命中程序性记忆")
            return ChatResult(answer=proc, source="procedural")

        _p(f"长时记忆：向量检索（场景 {self.working.scene}）…")
        query_tags = parse_tags_from_text(text)
        hits = self.long_term.search_hits(
            text,
            query_vec=vec,
            scene=self.working.scene,
            tags=query_tags or None,
        )
        if hits:
            from .working import is_non_reusable_answer

            long_res = self.long_term.format_hit_answers(hits)
            if is_non_reusable_answer(long_res):
                _p("长时命中为失败类答复，忽略并继续")
                hits = []
                long_res = None
            else:
                self.long_term.reinforce_hits(hits)
                hit_meta = [h.as_dict() for h in hits]
                self._write_back(text, keywords, vec, long_res, persist_long=False)
                _p("命中长时记忆")
                return ChatResult(
                    answer=long_res,
                    source="long_term",
                    meta={
                        "hit_local": True,
                        "hits": hit_meta,
                        "explain": hit_meta[0]["reasons"] if hit_meta else [],
                    },
                )

        _p("本地三级记忆未命中")
        return ChatResult(
            answer="",
            source="miss",
            meta={"hit_local": False},
        )

    def chat(
        self,
        input_text: str,
        stream_callback=None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ChatResult:
        """
        标准调用链路。stream_callback 预留流式适配（当前同步返回）。
        on_progress：阶段进度（CLI 交互模式等可打印到 stderr）。
        Web/CLI 等本地入口：本地记忆未命中时可回退沙箱内 LLM。
        """
        local = self.ask_local(input_text, on_progress=on_progress)
        # 指令 / 本地命中 / 感觉层拒绝：直接返回
        if local.source != "miss":
            if stream_callback:
                stream_callback(local.answer)
            return local

        text = (input_text or "").strip()
        keywords = extract_keywords(text)
        vec = self.embedder.embed(text)

        # 沙箱无解 → 大模型（仅本地 App/CLI；MCP 应走 ask_local）
        if self.llm is None:
            answer = "沙箱无匹配记忆，且大模型未启用（config.llm.enabled=false）。"
            result = ChatResult(answer=answer, source="llm", meta={"skipped": True})
            if on_progress:
                on_progress("大模型未启用，跳过")
            if stream_callback:
                stream_callback(result.answer)
            return result

        provider = getattr(self.config.llm, "provider", "") or "llm"
        runtime = getattr(self.config.llm, "runtime", "") or ""
        if on_progress:
            hint = f"provider={provider}"
            if str(provider).lower() in {"cursor", "cursor_cloud", "cursor-agent"}:
                from .llm import describe_cursor_llm

                hint = describe_cursor_llm(self.config.llm)
            elif runtime:
                hint += f" runtime={runtime}"
            on_progress(f"本地无解 → 回退沙箱 LLM（{hint}）…")

        # 飞书链接：在沙箱内用 OpenAPI 拉正文，注入 LLM 上下文（不依赖 Cursor MCP）
        context = self.working.recent_context_text()
        feishu_cfg = getattr(self.config, "feishu", None)
        if feishu_cfg is not None:
            from .feishu import extract_feishu_urls, feishu_configured, fetch_feishu_docs_for_text

            refs = extract_feishu_urls(text)
            if refs:
                if not feishu_configured(feishu_cfg):
                    if on_progress:
                        on_progress("检测到飞书链接，但未配置 feishu.* 凭证，跳过拉取")
                else:
                    if on_progress:
                        on_progress(f"飞书文档：拉取 {len(refs)} 篇…")
                    results, feishu_ctx = fetch_feishu_docs_for_text(
                        feishu_cfg, text, config_path=self.config_path
                    )
                    ok_n = sum(1 for r in results if r.ok)
                    if on_progress:
                        on_progress(f"飞书文档：成功 {ok_n}/{len(results)}")
                    if feishu_ctx:
                        context = (
                            (context + "\n\n" if context else "")
                            + "【飞书文档正文（由记忆沙箱拉取）】\n"
                            + feishu_ctx
                        )

        llm_res = self.llm.generate(text, context=context, on_progress=on_progress)

        self._write_back(text, keywords, vec, llm_res, persist_long=True)
        result = ChatResult(answer=llm_res, source="llm")
        if stream_callback:
            stream_callback(result.answer)
        return result

    def remember(
        self,
        question: str,
        answer: str,
        scene: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        kind: Optional[str] = None,
        facts: Optional[dict] = None,
    ) -> str:
        from .question_optimize import optimize_question

        scene = scene or self.working.scene
        opt = optimize_question(question)
        # 正文里的 #tag + 显式 tags 合并
        merged = merge_tags(parse_tags_from_text(question), parse_tags_from_text(answer), tags)
        # 向量交给 save_memory 用 embed_text 生成，覆盖更多口语变体
        rec = self.long_term.save_memory(
            question,
            answer,
            scene=scene,
            tags=merged,
            kind=kind,
            facts=facts,
        )
        self.working.add_context(opt.canonical, opt.keywords, rec.vector, role="user")
        self.working.add_context(rec.answer, extract_keywords(rec.answer), role="assistant")
        alias_hint = ""
        if opt.canonical != opt.original:
            alias_hint = f"；已优化为「{opt.canonical}」"
        tag_hint = f"；tags={','.join(rec.tags)}" if rec.tags else ""
        kind_hint = f"；kind={rec.kind}" if rec.kind and rec.kind != "qa" else ""
        scrub_hint = "；已脱敏" if (rec.meta or {}).get("scrubbed") else ""
        return (
            f"已写入长时记忆 [{rec.id}]（场景: {rec.scene}{alias_hint}{tag_hint}{kind_hint}{scrub_hint}；"
            f"别名 {len(rec.meta.get('aliases') or [])} 条）"
        )

    def extract_candidates(
        self,
        text: str,
        *,
        max_n: int = 3,
        tags: Optional[Sequence[str]] = None,
    ) -> dict:
        """从终端/日志文本提炼候选记忆（不写盘）。"""
        cands = extract_memory_candidates(text, max_n=max_n, tags=tags)
        suggested = suggest_tags_from_text(text)
        return {
            "candidates": [c.as_dict() for c in cands],
            "suggested_tags": suggested,
            "hint": "请确认后再 memory_remember；密钥类内容写入时会自动脱敏。",
        }

    def export_pack(
        self,
        *,
        name: str = "memory-pack",
        dest: Optional[str] = None,
        description: str = "",
        tags: Optional[Sequence[str]] = None,
        filter_tags: Optional[Sequence[str]] = None,
        filter_scene: Optional[str] = None,
        limit: int = 500,
    ) -> str:
        """导出可分享知识包（无向量，已脱敏）。"""
        from .pack import build_pack, write_pack

        self.long_term.reload()
        pack = build_pack(
            self.long_term.records,
            name=name,
            description=description,
            tags=tags,
            filter_tags=filter_tags,
            filter_scene=filter_scene,
            scrub=True,
            limit=limit,
        )
        out_dir = dest or str(self.long_term.persist_dir / "packs")
        path = write_pack(pack, out_dir)
        return f"已导出知识包 {pack.name}（{len(pack.records)} 条）→ {path}"

    def import_pack(
        self,
        path: str,
        *,
        merge: bool = True,
        confirm: bool = False,
    ) -> str:
        """导入知识包。merge=False 会清空现有长时记忆，需 confirm=True。"""
        from .pack import load_pack

        if not merge and not confirm:
            return "覆盖导入需 confirm=true（将清空现有长时记忆后再导入）。"
        pack = load_pack(path)
        stats = self.long_term.import_pack_records(
            pack.records, merge=merge, default_scene=self.working.scene
        )
        mode = "合并" if merge else "覆盖"
        return (
            f"已{mode}导入知识包「{pack.name}」：写入 {stats['imported']} 条，"
            f"当前共 {stats['total']} 条"
        )

    def archive_stale(
        self,
        *,
        min_hits: Optional[int] = None,
        older_than_days: Optional[float] = None,
        confirm: bool = False,
    ) -> str:
        """归档久未命中的长时记忆。"""
        if not confirm:
            days = older_than_days if older_than_days is not None else self.long_term.aging_days
            hits = min_hits if min_hits is not None else self.long_term.aging_min_hits
            return (
                f"将归档 hit_count≤{hits} 且超过 {days} 天未更新的条目。"
                "请再次调用并传 confirm=true。"
            )
        n = self.long_term.archive_low_access(
            min_hits=min_hits, older_than_days=older_than_days
        )
        return f"已归档 {n} 条低访问记忆 → {self.long_term.persist_dir / 'declarative_archive.jsonl'}"

    def list_packs(self) -> dict:
        """列出本地已导出的知识包。"""
        from .pack import list_local_packs, packs_dir

        items = list_local_packs(self.long_term.persist_dir)
        return {
            "packs": items,
            "dir": str(packs_dir(self.long_term.persist_dir)),
            "hint": "把 JSON 文件发给同事后，用 memory_import_pack / pack-import 合并导入。",
        }

    def check_git_changes(
        self,
        cwd: Optional[str] = None,
        *,
        since_ref: str = "HEAD~20",
        max_files: int = 80,
        limit: int = 8,
    ) -> dict:
        """对照 Git 变更，找出可能过时的记忆（只读 git）。"""
        from .git_sense import find_stale_memories, list_changed_paths, resolve_git_root

        root = resolve_git_root(cwd)
        if root is None:
            return {
                "git_root": None,
                "changed_paths": [],
                "stale": [],
                "hint": "当前目录不是 git 仓库（或找不到 git）。可传 cwd 为项目根路径。",
            }
        self.long_term.reload()
        changed = list_changed_paths(
            str(root), since_ref=since_ref, max_files=max_files
        )
        stale = find_stale_memories(self.long_term.records, changed, limit=limit)
        return {
            "git_root": str(root),
            "changed_paths": changed,
            "stale": [h.as_dict() for h in stale],
            "hint": (
                "若 stale 非空，请核对后更新或 forget 对应记忆；"
                "确认仍有效可用 memory_remember 强化。"
                if stale
                else "未发现与近期变更强相关的记忆，或仓库暂无匹配文件变更。"
            ),
        }

    def suggest_review_notes(
        self,
        cwd: Optional[str] = None,
        *,
        max_hints: int = 3,
    ) -> dict:
        """从近期 commit 提炼可沉淀的协作习惯候选（不写盘）。"""
        from .git_sense import suggest_review_habits

        hints = suggest_review_habits(cwd, max_hints=max_hints)
        return {
            "candidates": [h.as_dict() for h in hints],
            "hint": "确认后请 memory_remember；适合记团队约定 / Review 偏好。",
        }

    def bookmark_feishu(self, text: str) -> dict:
        """拉取飞书链接正文，生成待确认记忆候选（不写盘）。"""
        from .feishu_bookmark import build_feishu_bookmark_candidates

        cfg = getattr(self.config, "feishu", None)
        return build_feishu_bookmark_candidates(
            cfg, text, config_path=self.config_path
        )

    def forget(self, keyword: Optional[str] = None, layer: str = "all") -> str:
        """主动遗忘。layer: sensory|working|long_term|all"""
        counts = {}
        if layer in ("sensory", "all"):
            counts["sensory"] = self.sensory.forget(keyword)
        if layer in ("working", "all"):
            counts["working"] = self.working.forget(keyword)
        if layer in ("long_term", "all"):
            counts["long_term"] = self.long_term.forget(keyword)
        if keyword is None:
            return f"已清空记忆层 {layer}: {counts}"
        return f"已按关键词「{keyword}」遗忘 {layer}: {counts}"

    def backup_long_term(self, dest: Optional[str] = None) -> str:
        """手动备份长时陈述性记忆。"""
        self.long_term.reload()
        path = self.long_term.backup_declarative(dest)
        n = len(self.long_term.records)
        return f"已备份长时记忆 {n} 条 → {path}"

    def restore_long_term(self, path: Optional[str] = None) -> str:
        """从备份恢复长时陈述性记忆（覆盖当前）。"""
        try:
            n = self.long_term.restore_declarative(path)
        except FileNotFoundError as e:
            return f"恢复失败：{e}"
        except ValueError as e:
            return f"恢复失败：{e}"
        return f"已从备份恢复长时记忆 {n} 条" + (f"（{path}）" if path else "（最新备份）")

    def clear_long_term(self, *, backup_first: bool = False) -> str:
        """清空长时陈述性记忆（供已确认的 UI/API 调用）。"""
        self.long_term.reload()
        backup_msg = ""
        if backup_first and self.long_term.records:
            backup_msg = self.backup_long_term() + "\n"
        n = self.long_term.forget()
        return (
            f"{backup_msg}长时记忆已清空（移除陈述性记忆 {n} 条）。程序性模板保留。"
        )

    def delete_memory(self, memory_id: str = "", question: str = "") -> str:
        """删除单条长时记忆：优先 id，其次精确匹配问题。"""
        if memory_id:
            removed = self.long_term.delete_by_id(memory_id)
            if removed:
                return f"已删除记忆 [{removed.id}]：{removed.question}"
            return f"未找到 id={memory_id} 的记忆"
        q = (question or "").strip()
        if not q:
            return "请提供 memory_id 或 question"
        self.long_term.reload()
        for rec in list(self.long_term.records):
            aliases = [rec.question] + list((rec.meta or {}).get("aliases") or [])
            if q == rec.question or q in aliases:
                self.long_term.delete_by_id(rec.id)
                return f"已删除记忆 [{rec.id}]：{rec.question}"
        # 退化：关键词删除 1 条最相近
        n = self.long_term.forget(q)
        if n:
            return f"已按关键词删除 {n} 条与「{q}」相关的记忆"
        return f"未找到问题「{q}」对应的记忆"

    def set_agent_mode(self, ui_mode: str, persist: bool = True) -> str:
        """
        切换本地 Cursor Agent 模式：ask（只读）| plan（规划）| agent（可写全工具）。
        立即作用于后续 chat LLM 回退；可选持久化到用户 config.yaml。
        """
        from .config import apply_agent_ui_mode, persist_llm_agent_settings

        ui = apply_agent_ui_mode(self.config.llm, ui_mode)
        path_hint = ""
        if persist:
            path = persist_llm_agent_settings(
                getattr(self, "config_path", None),
                self.config.llm.agent_mode,
                self.config.llm.agent_force,
            )
            path_hint = f"；已写入 {path}"
        labels = {
            "ask": "Ask 只读（可读盘，不改文件）",
            "plan": "Plan 规划（只读规划）",
            "agent": "Agent 全工具（可改文件/执行命令，慎用）",
        }
        return f"已切换为 {labels.get(ui, ui)}{path_hint}"

    def status(self) -> dict:
        from .config import agent_ui_mode_from_config

        return {
            "sensory": self.sensory.stats(),
            "working": self.working.stats(),
            "long_term": self.long_term.stats(),
            "llm": {
                "enabled": self.config.llm.enabled,
                "provider": self.config.llm.provider,
                "runtime": getattr(self.config.llm, "runtime", ""),
                "agent_mode": agent_ui_mode_from_config(self.config.llm),
                "agent_force": bool(self.config.llm.agent_force),
                "cwd": getattr(self.config.llm, "cwd", "") or "",
            },
            "feishu": self._feishu_status(),
        }

    def _feishu_status(self) -> dict:
        from .feishu import feishu_configured

        cfg = getattr(self.config, "feishu", None)
        if cfg is None:
            return {"enabled": False, "configured": False}
        return {
            "enabled": bool(cfg.enabled),
            "configured": feishu_configured(cfg),
            "api_base": (cfg.api_base or "")[:80],
            "has_app_id": bool((cfg.app_id or "").strip()),
            "has_user_token": bool((cfg.user_access_token or "").strip()),
            "has_refresh_token": bool((getattr(cfg, "refresh_token", "") or "").strip()),
            "redirect_uri": (getattr(cfg, "redirect_uri", "") or "")[:120],
        }

    def list_working(self) -> list:
        """短时/工作记忆窗口内容（不含向量）。"""
        items = []
        for i, item in enumerate(self.working.window, 1):
            items.append({
                "index": i,
                "role": item.get("role", "user"),
                "text": item.get("text", ""),
                "keywords": item.get("kw") or [],
            })
        return items

    def list_long_term(self, limit: int = 200) -> dict:
        """长时记忆：陈述性 + 程序性（不含向量）。"""
        self.long_term.reload()
        declarative = []
        for rec in self.long_term.records[: max(0, limit)]:
            declarative.append({
                "id": rec.id,
                "question": rec.question,
                "answer": rec.answer,
                "scene": rec.scene,
                "tags": list(rec.tags or []),
                "kind": rec.kind or "qa",
                "facts": dict(rec.facts or {}),
                "weight": round(rec.weight, 3),
                "hit_count": rec.hit_count,
                "keywords": rec.keywords,
            })
        return {
            "declarative": declarative,
            "declarative_total": len(self.long_term.records),
            "procedural": dict(self.long_term.procedural),
            "persist_dir": str(self.long_term.persist_dir),
        }

    def format_memory_view(self, layer: str = "all") -> str:
        """人类可读的记忆清单，供 UI / MCP 展示。"""
        parts = []
        if layer in ("working", "all", "short"):
            items = self.list_working()
            parts.append(f"【工作记忆 / 短时】{len(items)}/{self.working.max_size} · 场景 {self.working.scene}")
            if not items:
                parts.append("  （空）")
            else:
                for it in items:
                    role = "用户" if it["role"] == "user" else "助手"
                    parts.append(f"  {it['index']}. [{role}] {it['text']}")
        if layer in ("long_term", "all", "long"):
            data = self.list_long_term()
            parts.append("")
            parts.append(
                f"【长时记忆 / 陈述性】{data['declarative_total']} 条 · {data['persist_dir']}"
            )
            if not data["declarative"]:
                parts.append("  （空）")
            else:
                for i, rec in enumerate(data["declarative"], 1):
                    tag_s = f" #{' #'.join(rec['tags'])}" if rec.get("tags") else ""
                    kind_s = f"/{rec['kind']}" if rec.get("kind") and rec["kind"] != "qa" else ""
                    parts.append(
                        f"  {i}. [{rec['scene']}{kind_s}{tag_s}] Q: {rec['question']}\n"
                        f"     A: {rec['answer']}  "
                        f"(命中{rec['hit_count']} 权重{rec['weight']})"
                    )
            parts.append("")
            parts.append(f"【长时记忆 / 程序性】{len(data['procedural'])} 条")
            if not data["procedural"]:
                parts.append("  （空）")
            else:
                for name, tpl in data["procedural"].items():
                    preview = tpl if len(tpl) <= 80 else tpl[:80] + "…"
                    parts.append(f"  · {name}: {preview}")
        return "\n".join(parts).strip()

    # ---------- internals ----------
    def _write_back(
        self,
        question: str,
        keywords,
        vec,
        answer: str,
        persist_long: bool,
    ) -> None:
        # 失败/鉴权类答复不进工作记忆与长时，否则删长时后仍会「命中工作记忆」假复用
        from .working import is_non_reusable_answer

        if is_non_reusable_answer(answer):
            return
        self.working.add_context(question, keywords, vec, role="user")
        self.working.add_context(answer, extract_keywords(answer), role="assistant")
        if persist_long and answer:
            self.long_term.save_memory(
                question,
                answer,
                scene=self.working.scene,
                vector=vec,
            )
        # 高频短问答沉淀进工作记忆 FAQ，进一步减少模型调用
        if persist_long and len(question) <= 20 and len(answer) <= 80:
            hits = self.long_term.search_hits(question, query_vec=vec, threshold=0.9, top_k=1)
            if hits and hits[0].record.hit_count >= 3:
                self.working.rule_engine.add_faq(question, answer)

    def _handle_command(self, raw: str) -> Optional[ChatResult]:
        text = (raw or "").strip()
        if not text:
            return None

        # 记住：问 => 答  /  记住 问 => 答
        m = re.match(
            r"^(?:记住[:：]\s*|记住\s+)(.+?)\s*(?:=>|→|->|＝|=)\s*(.+)$",
            text,
            re.DOTALL,
        )
        if m:
            msg = self.remember(m.group(1).strip(), m.group(2).strip())
            return ChatResult(answer=msg, source="command")

        # 记住：纯知识片段
        m = re.match(r"^记住[:：]\s*(.+)$", text, re.DOTALL)
        if m:
            content = m.group(1).strip()
            msg = self.remember(content, content)
            return ChatResult(answer=msg, source="command")

        # 忘记刚才内容
        if text in {"忘记刚才内容", "忘记刚才", "忘掉刚才"}:
            n = self.working.forget()
            self.sensory.clear()
            return ChatResult(
                answer=f"已清空工作记忆与感觉记忆（工作记忆移除 {n} 条）。",
                source="command",
            )

        m = re.match(r"^忘记[:：]\s*(.+)$", text)
        if m:
            return ChatResult(answer=self.forget(m.group(1).strip()), source="command")

        m = re.match(r"^(?:删除记忆|删掉记忆|移除记忆)[:：]\s*(.+)$", text)
        if m:
            target = m.group(1).strip()
            if re.fullmatch(r"[0-9a-fA-F]{8,16}", target):
                return ChatResult(answer=self.delete_memory(memory_id=target), source="command")
            return ChatResult(answer=self.delete_memory(question=target), source="command")

        if text in {"清空工作记忆", "清空上下文"}:
            self.working.clear()
            return ChatResult(answer="工作记忆已清空。", source="command")

        if text in {"飞书登录", "飞书授权", "登录飞书"}:
            try:
                from .config import load_config
                from .feishu_oauth import run_oauth_login
                from .paths import app_support_dir

                user_cfg = str(app_support_dir() / "config.yaml")
                _, path = run_oauth_login(
                    self.config.feishu,
                    config_path=user_cfg,
                    open_browser=True,
                )
                fresh = load_config(user_cfg)
                self.config.feishu = fresh.feishu
                return ChatResult(
                    answer=(
                        f"飞书授权成功，token 已写入 {path}。"
                        "之后过期会自动 refresh；失效再发「飞书登录」。"
                    ),
                    source="command",
                )
            except Exception as e:
                return ChatResult(
                    answer=(
                        f"飞书登录失败：{e}\n"
                        "也可在终端执行：python3 scripts/feishu_login.py\n"
                        "注意：user_access_token 不能在管理后台查看明文，必须浏览器授权换取。"
                    ),
                    source="command",
                )

        if text in {"备份长时记忆", "备份长期记忆", "导出长时记忆", "backup long term"}:
            return ChatResult(answer=self.backup_long_term(), source="command")

        m = re.match(r"^(?:备份长时记忆|备份长期记忆|导出长时记忆)[:：]\s*(.+)$", text)
        if m:
            return ChatResult(answer=self.backup_long_term(m.group(1).strip()), source="command")

        if text in {"恢复长时记忆备份", "从备份恢复长时记忆", "恢复最新长时备份"}:
            return ChatResult(answer=self.restore_long_term(), source="command")

        m = re.match(r"^(?:恢复长时记忆备份|从备份恢复长时记忆)[:：]\s*(.+)$", text)
        if m:
            return ChatResult(answer=self.restore_long_term(m.group(1).strip()), source="command")

        if text in {"查看长时备份", "长时记忆备份列表", "列出长时备份"}:
            backups = self.long_term.list_backups()
            if not backups:
                return ChatResult(answer="暂无长时记忆备份。可用「备份长时记忆」创建。", source="command")
            lines = [f"共 {len(backups)} 份备份（新→旧）："]
            for i, p in enumerate(backups[:20], 1):
                lines.append(f"{i}. {p}")
            return ChatResult(answer="\n".join(lines), source="command")

        if text in {"清空长时记忆", "清空长期记忆", "清空持久记忆"}:
            self.long_term.reload()
            n = len(self.long_term.records)
            return ChatResult(
                answer=(
                    f"即将清空全部长时记忆（当前 {n} 条陈述性问答）。此操作不可撤销。\n"
                    "请发送「确认清空长时记忆」执行；\n"
                    "建议先发送「备份长时记忆」；\n"
                    "也可发送「确认清空长时记忆并备份」以先备份再清空。"
                ),
                source="command",
                meta={"needs_confirm": True, "action": "clear_long_term", "count": n},
            )

        if text in {
            "确认清空长时记忆",
            "清空长时记忆：确认",
            "清空长时记忆:确认",
            "确认清空长期记忆",
        }:
            return ChatResult(answer=self.clear_long_term(backup_first=False), source="command")

        if text in {
            "确认清空长时记忆并备份",
            "确认清空长时记忆（先备份）",
            "确认清空长时记忆(先备份)",
        }:
            return ChatResult(answer=self.clear_long_term(backup_first=True), source="command")

        # 清理低访问记忆：可选参数「清理低访问记忆 30」表示超过 N 天且命中次数≤0
        m = re.match(r"^(?:清理低访问记忆|归档长时记忆|遗忘低访问记忆)(?:\s+(\d+))?$", text)
        if m:
            days = float(m.group(1) or 30)
            n = self.long_term.archive_low_access(min_hits=0, older_than_days=days)
            return ChatResult(
                answer=f"已清理低访问长时记忆 {n} 条（命中次数≤0 且超过 {int(days)} 天未更新）。",
                source="command",
            )

        if text in {"优化已有记忆", "重优化记忆", "刷新记忆索引"}:
            n = self.long_term.reoptimize_all()
            return ChatResult(
                answer=f"已对 {n} 条长时记忆重新优化问题（补全别名并刷新向量）。",
                source="command",
            )

        if text in {"清空全部记忆", "清空所有记忆"}:
            self.long_term.reload()
            n = len(self.long_term.records)
            return ChatResult(
                answer=(
                    f"即将清空全部记忆（含工作/感觉，以及 {n} 条长时陈述性问答）。此操作不可撤销。\n"
                    "请发送「确认清空全部记忆」执行；\n"
                    "建议先「备份长时记忆」；\n"
                    "或发送「确认清空全部记忆并备份」。"
                ),
                source="command",
                meta={"needs_confirm": True, "action": "clear_all", "count": n},
            )

        if text in {"确认清空全部记忆", "清空全部记忆：确认", "清空全部记忆:确认"}:
            n_w = self.working.forget()
            n_s = self.sensory.forget()
            n_l = self.long_term.forget()
            return ChatResult(
                answer=(
                    f"已清空全部记忆：感觉 {n_s} · 工作 {n_w} · 长时陈述性 {n_l}。"
                    "程序性模板保留。"
                ),
                source="command",
            )

        if text in {"确认清空全部记忆并备份", "确认清空全部记忆（先备份）", "确认清空全部记忆(先备份)"}:
            backup_msg = ""
            if self.long_term.records:
                backup_msg = self.backup_long_term() + "\n"
            n_w = self.working.forget()
            n_s = self.sensory.forget()
            n_l = self.long_term.forget()
            return ChatResult(
                answer=(
                    f"{backup_msg}已清空全部记忆：感觉 {n_s} · 工作 {n_w} · 长时陈述性 {n_l}。"
                    "程序性模板保留。"
                ),
                source="command",
            )

        if text in {"查看记忆状态", "记忆状态", "memory status", "status"}:
            import json
            return ChatResult(
                answer=json.dumps(self.status(), ensure_ascii=False, indent=2),
                source="command",
            )

        if text in {"查看短时记忆", "查看工作记忆", "短时记忆", "工作记忆"}:
            return ChatResult(answer=self.format_memory_view("working"), source="command")

        if text in {"查看长时记忆", "长时记忆"}:
            return ChatResult(answer=self.format_memory_view("long_term"), source="command")

        if text in {"查看全部记忆", "查看记忆", "全部记忆"}:
            return ChatResult(answer=self.format_memory_view("all"), source="command")

        m = re.match(r"^切换场景[:：]\s*(.+)$", text)
        if m:
            self.working.set_scene(m.group(1).strip())
            return ChatResult(
                answer=f"已切换场景为「{self.working.scene}」。同场景记忆检索将优先。",
                source="command",
            )

        m = re.match(
            r"^(?:切换Agent模式|切换agent模式|Agent模式|agent模式|切换LLM模式)[:：]?\s*(.*)$",
            text,
            re.IGNORECASE,
        )
        if m:
            raw = (m.group(1) or "").strip()
            if not raw:
                from .config import agent_ui_mode_from_config

                cur = agent_ui_mode_from_config(self.config.llm)
                return ChatResult(
                    answer=(
                        f"当前 Agent 模式：{cur}\n"
                        "切换：切换Agent模式：ask | plan | agent\n"
                        "ask=只读，plan=规划，agent=可写全工具（慎用）"
                    ),
                    source="command",
                )
            try:
                msg = self.set_agent_mode(raw, persist=True)
            except ValueError as e:
                return ChatResult(answer=str(e), source="command")
            return ChatResult(answer=msg, source="command")

        return None
