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
SERVER_VERSION = "0.1.5"

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
            "命中则直接采用 answer；未命中（hit_local=false）由当前 AI 工具自己的模型继续推理，"
            "结束后 memory_remember。纯管理指令可跳过本工具。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户原话 / 想解决的问题（不必手写「记录到长期记忆」）",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_ask",
        "description": (
            "向本地记忆沙箱提问：仅检索感觉/工作/长时记忆，绝不调用沙箱内 LLM。"
            "命中则直接返回答案；未命中 hit_local=false，由当前 AI 工具自己的模型继续。"
            "一般对话请优先用 memory_prepare（会自动拼接「记录到长期记忆」）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题或检索语句"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_remember",
        "description": (
            "把一条问答/知识点写入长时记忆，供后续 memory_ask 命中。"
            "适合固化：启动命令、环境注意点、踩坑结论、团队约定。"
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
            },
            "required": ["question", "answer"],
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


def _ask_payload(query: str) -> Dict[str, Any]:
    """MCP 专用：只读本地记忆，不调用沙箱内 LLM。"""
    _scrub_mock_working()
    result = _sandbox().ask_local(query)
    hit_local = result.source not in ("miss", "sensory_reject", "llm")
    return {
        "answer": result.answer,
        "source": result.source,
        "hit_local": hit_local,
        "hint": (
            "已命中本地记忆，可直接采用答案，无需再大段推理。"
            if hit_local
            else "本地记忆未命中（未调用沙箱 LLM）；请用当前 AI 工具自己的模型继续推理/查代码，"
            "结束后可用 memory_remember 固化结论。"
        ),
    }


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
            assembled = assemble_long_term_query(original)
            ask = _ask_payload(assembled)
            # 未命中时再用原话做本地检索，兼容库里未带后缀的旧条目（避免二次调 LLM）
            if not ask["hit_local"] and assembled != original:
                hits = sb.long_term.search_hits(original, top_k=1)
                if hits:
                    rec, _score = hits[0]
                    ask = {
                        "answer": rec.answer,
                        "source": "long_term",
                        "hit_local": True,
                        "hint": "已命中本地记忆，可直接采用答案，无需再大段推理。",
                    }
            if ask["hit_local"]:
                hint = (
                    "已命中本地记忆，直接采用 answer 回答用户，不要用模型默认身份覆盖。"
                    "本轮结束前仍须 memory_remember（可同问同答强化；若排障/改代码补充了新结论则写入更新）。"
                )
            else:
                hint = (
                    f"本地未命中。请以 assembled「{assembled}」作为待解决问题继续推理/查代码/排障；"
                    "本轮结束前必须 memory_remember（排障与改代码也要记；"
                    "question 用精简问法或 original，answer 为结论，scene=dev）。"
                )
            payload = {
                "original": original,
                "assembled": assembled,
                "answer": ask["answer"],
                "source": ask["source"],
                "hit_local": ask["hit_local"],
                "hint": hint,
            }
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_ask":
            query = (args.get("query") or "").strip()
            if not query:
                return _tool_result("query 不能为空", is_error=True)
            payload = _ask_payload(query)
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_remember":
            q = (args.get("question") or "").strip()
            a = (args.get("answer") or "").strip()
            scene = (args.get("scene") or "dev").strip() or "dev"
            if not q or not a:
                return _tool_result("question/answer 不能为空", is_error=True)
            msg = sb.remember(q, a, scene=scene)
            return _tool_result(msg)

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
