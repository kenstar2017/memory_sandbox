#!/usr/bin/env python3
"""
记忆沙箱 MCP Server（stdio / JSON-RPC）
供 Cursor Agent 调用：优先查本地三级记忆，再决定是否深入推理。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import load_config
from core.paths import app_support_dir, default_config_path, default_persist_dir
from core.utils import assemble_long_term_query

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memory-sandbox"
SERVER_VERSION = "0.1.10"

# 懒加载：initialize / tools/list 不触盘，避免多窗口 createClient 卡在启动
_SANDBOX: Optional[MemorySandbox] = None


def _build_sandbox() -> MemorySandbox:
    cfg_path = str(default_config_path())
    cfg = load_config(cfg_path)
    # 与 GUI/Web 共用同一份用户记忆
    cfg.long_term.persist_dir = str(default_persist_dir())
    return MemorySandbox(config=cfg, config_path=cfg_path)


def _sandbox() -> MemorySandbox:
    global _SANDBOX
    if _SANDBOX is None:
        _SANDBOX = _build_sandbox()
    return _SANDBOX


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "memory_prepare",
        "description": (
            "每轮对话的首选入口：把用户问题拼成「xxxx，记录到长期记忆。」并只检索本地三级记忆"
            "（感觉/工作/长时），绝不调用记忆沙箱内的 LLM。"
            "始终返回 references/context_pack（多条软召回参考问答）供结合当前项目上下文使用；"
            "hit_local=true 时另有 answer。改代码/做功能时以仓库为准、沙箱仅作参考；"
            "结束后 memory_remember。纯管理指令可跳过本工具。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户原话 / 想解决的问题（不必手写「记录到长期记忆」；可用 #tag 过滤）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：仅在这些标签内检索",
                },
                "ref_top_k": {
                    "type": "integer",
                    "description": "软召回参考问答条数，默认 5，最大 20",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_ask",
        "description": (
            "向本地记忆沙箱提问：仅检索感觉/工作/长时记忆，绝不调用沙箱内 LLM。"
            "返回 hit_local/answer，并始终附带 references/context_pack 多条参考问答。"
            "一般对话请优先用 memory_prepare（会自动拼接「记录到长期记忆」）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题或检索语句（可用 #tag）"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：仅在这些标签内检索",
                },
                "ref_top_k": {
                    "type": "integer",
                    "description": "软召回参考问答条数，默认 5，最大 20",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_remember",
        "description": (
            "把一条问答/知识点写入长时记忆，供后续 memory_prepare / memory_ask 命中。"
            "适合固化：启动命令、环境注意点、踩坑结论、团队约定。"
            "可用 tags / kind / facts；写入前自动脱敏 token/.env/密钥。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "问题或检索键"},
                "answer": {"type": "string", "description": "答案或知识内容"},
                "scene": {
                    "type": "string",
                    "description": "场景标签，如 dev / agency / general",
                    "default": "dev",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签列表，如 [\"feishu\", \"frontend\"]",
                },
                "kind": {
                    "type": "string",
                    "enum": ["qa", "command", "path", "env", "pitfall", "decision"],
                    "description": "结构化类型，默认 qa",
                },
                "facts": {
                    "type": "object",
                    "description": "可选结构化字段：command/path/env/pitfall/decision",
                },
            },
            "required": ["question", "answer"],
        },
    },
    {
        "name": "memory_extract",
        "description": (
            "从终端输出 / diff / 日志文本中启发式提炼 1~3 条候选记忆（不写盘）。"
            "确认后请对选中项调用 memory_remember。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "终端或日志原文"},
                "max_n": {
                    "type": "integer",
                    "description": "最多返回条数，默认 3",
                    "default": 3,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "建议打上的标签",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_forget",
        "description": (
            "按关键词遗忘记忆；不传 keyword 则清空指定层。"
            "清空 long_term 或 all 时必须传 confirm=true；建议先 memory_backup。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "要删除内容包含的关键词"},
                "layer": {
                    "type": "string",
                    "enum": ["all", "sensory", "working", "long_term"],
                    "default": "all",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "清空 long_term/all 且无 keyword 时必须为 true",
                    "default": False,
                },
                "backup_first": {
                    "type": "boolean",
                    "description": "清空前先备份长时记忆",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "memory_backup",
        "description": "手动备份长时陈述性记忆到本地 backups 目录（或指定路径）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dest": {
                    "type": "string",
                    "description": "可选：备份文件或目录路径；默认写到记忆目录 backups/",
                },
            },
        },
    },
    {
        "name": "memory_export_pack",
        "description": (
            "导出一份可发给同事的知识包（去掉向量、遮盖密钥）。可按标签/场景过滤。"
            "默认写到记忆目录 packs/。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "知识包名称", "default": "memory-pack"},
                "dest": {"type": "string", "description": "输出文件或目录"},
                "description": {"type": "string", "description": "包说明"},
                "filter_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只导出带这些标签的记忆",
                },
                "filter_scene": {"type": "string", "description": "只导出该 scene"},
                "limit": {"type": "integer", "default": 500},
            },
        },
    },
    {
        "name": "memory_import_pack",
        "description": "导入同事发来的知识包。默认合并；若要清空再导入需 merge=false 且 confirm=true。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "知识包 JSON 路径"},
                "merge": {"type": "boolean", "default": True},
                "confirm": {
                    "type": "boolean",
                    "description": "merge=false 时必须为 true",
                    "default": False,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "memory_archive",
        "description": (
            "把很久没用过的长时记忆挪到归档文件，避免主库越堆越乱。"
            "需 confirm=true；默认天数见配置 aging_days。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "older_than_days": {"type": "number", "description": "超过多少天未更新"},
                "min_hits": {"type": "integer", "description": "命中次数上限（含）"},
                "confirm": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "memory_list_packs",
        "description": "列出本机已导出的知识包文件，方便发给同事或再次导入。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_git_check",
        "description": (
            "对照当前 Git 变更，找出可能已经过时的本地记忆（只读 git，不改仓库）。"
            "适合改完代码后扫一眼「旧经验还对不对」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {
                    "type": "string",
                    "description": "项目目录；省略则用当前进程目录",
                },
                "since_ref": {
                    "type": "string",
                    "description": "对比起点，默认 HEAD~20",
                    "default": "HEAD~20",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条可疑记忆",
                    "default": 8,
                },
            },
        },
    },
    {
        "name": "memory_review_suggest",
        "description": (
            "根据近期 git commit 信息，提示可沉淀的协作习惯/约定（不写盘）。"
            "确认后再 memory_remember。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "项目目录"},
                "max_hints": {"type": "integer", "default": 3},
            },
        },
    },
    {
        "name": "memory_feishu_bookmark",
        "description": (
            "把飞书文档链接拉成「待确认记忆」候选（需已配置并登录飞书）。"
            "确认后再 memory_remember；不会自动写入。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "含飞书 wiki/docx 链接的文本",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory_restore",
        "description": "从备份恢复长时记忆（覆盖当前）。不传 path 则恢复最新一份备份。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "备份文件路径；省略则用最新备份"},
                "confirm": {
                    "type": "boolean",
                    "description": "必须为 true 才执行恢复",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "删除单条已记住的问答。优先传 memory_id；否则传 question 精确匹配。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 id"},
                "question": {"type": "string", "description": "问题原文或核心问法"},
            },
        },
    },
    {
        "name": "memory_status",
        "description": "查看记忆沙箱各层统计与数据目录。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_list",
        "description": (
            "列出短时/工作记忆或长时记忆的具体内容。"
            "layer=working 看短时窗口；layer=long_term 看持久化问答；layer=all 全部。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "layer": {
                    "type": "string",
                    "enum": ["working", "long_term", "all"],
                    "default": "all",
                    "description": "要查看的记忆层",
                },
            },
        },
    },
    {
        "name": "memory_set_scene",
        "description": "切换工作记忆场景，同场景长时记忆检索加权。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scene": {"type": "string", "description": "场景名，如 dev"},
            },
            "required": ["scene"],
        },
    },
]


def _tool_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _scrub_mock_working() -> None:
    """避免历史 Mock 占位残留在工作记忆里干扰检索。"""
    sb = _sandbox()
    sb.working.window = [
        item
        for item in sb.working.window
        if not str(item.get("text", "")).startswith(("[MockLLM]", "[LLM Error]"))
    ]


def _parse_ref_top_k(raw: Any, default: int = 5) -> int:
    try:
        n = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, 20))


def _context_pack_from_dicts(
    refs: List[Dict[str, Any]],
    *,
    max_answer_chars: int = 800,
) -> str:
    """由 references dict 列表拼 context_pack（不依赖 SearchHit）。"""
    if not refs:
        return ""
    blocks: List[str] = [
        "【记忆沙箱 · 参考问答】以下为相关历史结论，供结合当前项目上下文使用；"
        "可能过时，以仓库/现状为准，勿直接当作最终实现。"
    ]
    for i, r in enumerate(refs, 1):
        ans = str(r.get("answer") or "").strip()
        if max_answer_chars > 0 and len(ans) > max_answer_chars:
            ans = ans[:max_answer_chars].rstrip() + "…"
        tags = r.get("tags") or []
        tag_s = f" tags={','.join(tags)}" if tags else ""
        reasons = r.get("reasons") or []
        reason_s = "/".join(reasons[:4]) if reasons else ""
        score = r.get("score")
        try:
            score_s = f"{float(score):.2f}"
        except (TypeError, ValueError):
            score_s = str(score or "")
        meta = f"score={score_s}{tag_s}"
        if reason_s:
            meta += f" reasons={reason_s}"
        blocks.append(
            f"### 参考问答 {i}\n"
            f"问：{r.get('question') or ''}\n"
            f"答：{ans}\n"
            f"（{meta}）"
        )
    return "\n\n".join(blocks)


def _attach_references(
    payload: Dict[str, Any],
    query: str,
    *,
    tags: Optional[List[str]] = None,
    ref_top_k: int = 5,
) -> Dict[str, Any]:
    """始终附带软召回参考问答（供 Cursor 结合项目上下文使用）。"""
    sb = _sandbox()
    pack = sb.build_reference_pack(query, tags=tags, top_k=ref_top_k)
    payload["references"] = pack["references"]
    payload["context_pack"] = pack["context_pack"]
    payload["ref_threshold"] = pack["ref_threshold"]
    return payload


def _hint_with_references(
    *,
    hit_local: bool,
    has_refs: bool,
    assembled: str = "",
) -> str:
    ref_note = (
        "请阅读 references / context_pack：把其中问答当「参考」纳入本轮推理，"
        "并结合当前仓库/用户上下文解决用户问题；参考可能过时，以仓库为准。"
        if has_refs
        else "无相关参考问答。"
    )
    if hit_local:
        return (
            "已有硬命中 answer。"
            "若用户只是复述事实、无需改代码，可优先采用 answer（勿用模型默认身份覆盖）。"
            "若在改功能/查代码/排障：不要因命中而短路，" + ref_note +
            "结束后 memory_remember。"
        )
    base = (
        f"本地未硬命中。请以 assembled「{assembled}」为待解决问题继续推理/查代码/排障；"
        if assembled
        else "本地未硬命中（未调用沙箱 LLM）；请用当前 AI 工具继续推理/查代码；"
    )
    return base + ref_note + "本轮结束前必须 memory_remember（scene=dev，可带 tags）。"


def _ask_payload(
    query: str,
    tags: Optional[List[str]] = None,
    *,
    ref_top_k: int = 5,
) -> Dict[str, Any]:
    """MCP 专用：只读本地记忆，不调用沙箱内 LLM；始终附带 references。"""
    _scrub_mock_working()
    sb = _sandbox()
    # 显式 tags：走长时检索并附带可解释命中；否则走完整三级 ask_local
    if tags:
        from core.tags import merge_tags, parse_tags_from_text

        merged = merge_tags(parse_tags_from_text(query), tags)
        hits = sb.long_term.search_hits(query, scene=sb.working.scene, tags=merged)
        if hits:
            sb.long_term.reinforce_hits(hits)
            hit_meta = [h.as_dict() for h in hits]
            payload = {
                "answer": sb.long_term.format_hit_answers(hits),
                "source": "long_term",
                "hit_local": True,
                "hits": hit_meta,
                "explain": hit_meta[0]["reasons"],
            }
        else:
            payload = {
                "answer": "",
                "source": "miss",
                "hit_local": False,
                "hits": [],
                "explain": [],
            }
    else:
        result = sb.ask_local(query)
        hit_local = result.source not in ("miss", "sensory_reject", "llm")
        hit_meta = list((result.meta or {}).get("hits") or [])
        payload = {
            "answer": result.answer,
            "source": result.source,
            "hit_local": hit_local,
            "hits": hit_meta,
            "explain": list((result.meta or {}).get("explain") or []),
        }

    _attach_references(payload, query, tags=tags, ref_top_k=ref_top_k)
    payload["hint"] = _hint_with_references(
        hit_local=bool(payload.get("hit_local")),
        has_refs=bool(payload.get("references")),
    )
    return payload


def call_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    args = arguments or {}
    try:
        sb = _sandbox()
        # App / 其它进程可能已更新磁盘记忆，工具调用前强制同步
        sb.long_term.reload()

        if name == "memory_prepare":
            original = (args.get("query") or "").strip()
            if not original:
                return _tool_result("query 不能为空", is_error=True)
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            ref_top_k = _parse_ref_top_k(args.get("ref_top_k"), 5)
            assembled = assemble_long_term_query(original)
            ask = _ask_payload(assembled, tags=tags, ref_top_k=ref_top_k)
            # 未命中时再用原话做本地检索，兼容库里未带后缀的旧条目（避免二次调 LLM）
            if not ask["hit_local"] and assembled != original:
                from core.tags import merge_tags, parse_tags_from_text

                merged = merge_tags(parse_tags_from_text(original), tags)
                hits = sb.long_term.search_hits(
                    original, scene=sb.working.scene, tags=merged or None, top_k=3
                )
                if hits:
                    hit_meta = [h.as_dict() for h in hits]
                    ask = {
                        "answer": sb.long_term.format_hit_answers(hits),
                        "source": "long_term",
                        "hit_local": True,
                        "hits": hit_meta,
                        "explain": hit_meta[0]["reasons"],
                    }
                    _attach_references(
                        ask, original, tags=tags, ref_top_k=ref_top_k
                    )
            # 原话软召回合并去重（按 id），再截断到 ref_top_k
            if assembled != original:
                pack_orig = sb.build_reference_pack(
                    original, tags=tags, top_k=ref_top_k
                )
                seen = {r.get("id") for r in (ask.get("references") or [])}
                for r in pack_orig["references"]:
                    rid = r.get("id")
                    if rid and rid not in seen:
                        ask.setdefault("references", []).append(r)
                        seen.add(rid)
                ask["ref_threshold"] = ask.get("ref_threshold") or pack_orig.get(
                    "ref_threshold"
                )

            refs = (ask.get("references") or [])[:ref_top_k]
            context_pack = _context_pack_from_dicts(refs) if refs else ""
            hint = _hint_with_references(
                hit_local=bool(ask["hit_local"]),
                has_refs=bool(refs),
                assembled=assembled,
            )
            payload = {
                "original": original,
                "assembled": assembled,
                "answer": ask["answer"],
                "source": ask["source"],
                "hit_local": ask["hit_local"],
                "hits": ask.get("hits") or [],
                "explain": ask.get("explain") or [],
                "references": refs,
                "context_pack": context_pack,
                "ref_threshold": ask.get("ref_threshold"),
                "hint": hint,
            }
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_ask":
            query = (args.get("query") or "").strip()
            if not query:
                return _tool_result("query 不能为空", is_error=True)
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            ref_top_k = _parse_ref_top_k(args.get("ref_top_k"), 5)
            payload = _ask_payload(query, tags=tags, ref_top_k=ref_top_k)
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_remember":
            q = (args.get("question") or "").strip()
            a = (args.get("answer") or "").strip()
            scene = (args.get("scene") or "dev").strip() or "dev"
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            kind = (args.get("kind") or "").strip() or None
            facts = args.get("facts") if isinstance(args.get("facts"), dict) else None
            if not q or not a:
                return _tool_result("question/answer 不能为空", is_error=True)
            msg = sb.remember(q, a, scene=scene, tags=tags, kind=kind, facts=facts)
            return _tool_result(msg)

        if name == "memory_extract":
            text = args.get("text") or ""
            if not str(text).strip():
                return _tool_result("text 不能为空", is_error=True)
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            max_n = args.get("max_n", 3)
            try:
                max_n = int(max_n)
            except (TypeError, ValueError):
                max_n = 3
            payload = sb.extract_candidates(str(text), max_n=max(1, min(max_n, 8)), tags=tags)
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_forget":
            keyword = args.get("keyword")
            layer = args.get("layer") or "all"
            confirm = bool(args.get("confirm"))
            backup_first = bool(args.get("backup_first"))
            # 全量清空长时/全部：必须二次确认
            if not keyword and layer in ("long_term", "all") and not confirm:
                n = len(sb.long_term.records)
                return _tool_result(
                    json.dumps(
                        {
                            "needs_confirm": True,
                            "count": n,
                            "hint": (
                                f"将清空 {n} 条长时记忆（layer={layer}）。"
                                "请再次调用并传 confirm=true；建议 backup_first=true。"
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    is_error=True,
                )
            if not keyword and layer in ("long_term", "all") and backup_first:
                if sb.long_term.records:
                    sb.backup_long_term()
            msg = sb.forget(keyword=keyword, layer=layer)
            return _tool_result(msg)

        if name == "memory_backup":
            dest = (args.get("dest") or "").strip() or None
            return _tool_result(sb.backup_long_term(dest))

        if name == "memory_export_pack":
            filter_tags = args.get("filter_tags")
            if isinstance(filter_tags, str):
                filter_tags = [filter_tags]
            try:
                limit = int(args.get("limit") or 500)
            except (TypeError, ValueError):
                limit = 500
            msg = sb.export_pack(
                name=(args.get("name") or "memory-pack").strip() or "memory-pack",
                dest=(args.get("dest") or "").strip() or None,
                description=(args.get("description") or "").strip(),
                filter_tags=filter_tags,
                filter_scene=(args.get("filter_scene") or "").strip() or None,
                limit=max(1, min(limit, 5000)),
            )
            return _tool_result(msg)

        if name == "memory_import_pack":
            path = (args.get("path") or "").strip()
            if not path:
                return _tool_result("path 不能为空", is_error=True)
            merge = args.get("merge")
            if merge is None:
                merge = True
            msg = sb.import_pack(
                path, merge=bool(merge), confirm=bool(args.get("confirm"))
            )
            err = msg.startswith("覆盖导入需")
            return _tool_result(msg, is_error=err)

        if name == "memory_archive":
            older = args.get("older_than_days")
            min_hits = args.get("min_hits")
            try:
                older_f = float(older) if older is not None else None
            except (TypeError, ValueError):
                older_f = None
            try:
                hits_i = int(min_hits) if min_hits is not None else None
            except (TypeError, ValueError):
                hits_i = None
            msg = sb.archive_stale(
                min_hits=hits_i,
                older_than_days=older_f,
                confirm=bool(args.get("confirm")),
            )
            needs = msg.startswith("将归档")
            return _tool_result(msg, is_error=needs)

        if name == "memory_list_packs":
            return _tool_result(json.dumps(sb.list_packs(), ensure_ascii=False, indent=2))

        if name == "memory_git_check":
            try:
                limit = int(args.get("limit") or 8)
            except (TypeError, ValueError):
                limit = 8
            payload = sb.check_git_changes(
                cwd=(args.get("cwd") or "").strip() or None,
                since_ref=(args.get("since_ref") or "HEAD~20").strip() or "HEAD~20",
                limit=max(1, min(limit, 30)),
            )
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_review_suggest":
            try:
                max_hints = int(args.get("max_hints") or 3)
            except (TypeError, ValueError):
                max_hints = 3
            payload = sb.suggest_review_notes(
                cwd=(args.get("cwd") or "").strip() or None,
                max_hints=max(1, min(max_hints, 10)),
            )
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_feishu_bookmark":
            text = (args.get("text") or "").strip()
            if not text:
                return _tool_result("text 不能为空", is_error=True)
            payload = sb.bookmark_feishu(text)
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_restore":
            if not bool(args.get("confirm")):
                return _tool_result(
                    "恢复会覆盖当前长时记忆。请再次调用并传 confirm=true。",
                    is_error=True,
                )
            path = (args.get("path") or "").strip() or None
            return _tool_result(sb.restore_long_term(path))

        if name == "memory_delete":
            msg = sb.delete_memory(
                memory_id=(args.get("memory_id") or "").strip(),
                question=(args.get("question") or "").strip(),
            )
            return _tool_result(msg, is_error=msg.startswith(("未找到", "请提供")))

        if name == "memory_status":
            st = sb.status()
            st["data_dir"] = str(app_support_dir())
            return _tool_result(json.dumps(st, ensure_ascii=False, indent=2))

        if name == "memory_list":
            layer = (args.get("layer") or "all").strip() or "all"
            text = sb.format_memory_view(layer)
            return _tool_result(text)

        if name == "memory_set_scene":
            scene = (args.get("scene") or "").strip()
            if not scene:
                return _tool_result("scene 不能为空", is_error=True)
            sb.working.set_scene(scene)
            return _tool_result(f"已切换场景为「{sb.working.scene}」")

        return _tool_result(f"未知工具: {name}", is_error=True)
    except Exception as e:
        return _tool_result(f"{e}\n{traceback.format_exc()}", is_error=True)


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # 通知：无响应
    if method and str(method).startswith("notifications/"):
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        result = call_tool(name, arguments)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # 未实现的方法
    if msg_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _write(obj: Dict[str, Any]) -> None:
    # MCP stdio：单行 NDJSON，禁止额外缩进/换行
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    # MCP 日志必须走 stderr，stdout 专供 JSON-RPC
    # 启动阶段不加载记忆库，尽快响应 initialize（多窗口 createClient 更稳）
    sys.stderr.write("[memory-sandbox] MCP ready (lazy sandbox)\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # 非 JSON 行（如误发的 Content-Length 头）直接忽略，避免干扰握手
        if not (line.startswith("{") or line.startswith("[")):
            sys.stderr.write(f"[memory-sandbox] skip non-json line: {line[:80]!r}\n")
            sys.stderr.flush()
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[memory-sandbox] parse error: {e}\n")
            sys.stderr.flush()
            continue

        # 支持简单批量（数组）
        if isinstance(msg, list):
            for item in msg:
                resp = handle(item)
                if resp is not None:
                    _write(resp)
            continue

        resp = handle(msg)
        if resp is not None:
            _write(resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
