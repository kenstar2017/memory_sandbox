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
SERVER_VERSION = "0.1.12"

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


def _fresh_feishu_cfg(sb: MemorySandbox) -> Any:
    """
    每次飞书调用都从磁盘重读凭据。

    MCP 是常驻进程、sandbox 是启动时建的单例，而 scripts/feishu_login.py 把新 token
    写在用户配置里。用缓存的话重新登录后工具会一直报「token 失效」，直到重启 MCP，
    而调用方根本看不出这是缓存问题。
    """
    try:
        fresh = load_config(str(default_config_path())).feishu
    except Exception:  # noqa: BLE001
        return sb.config.feishu
    sb.config.feishu = fresh
    return fresh


_MARKDOWN_HELP = (
    "Markdown 子集：#~###### 标题、-/* 无序列表、1. 有序列表、``` 代码块、> 引用、"
    "--- 分割线、GFM 表格（| a | b | 加 |---|---| 分隔行）；"
    "行内支持 **粗体**、*斜体*、~~删除线~~、`代码`、[文字](链接)。"
    "表格直接按 Markdown 写就行，会转成真表格；代码块里的 ** 和 | 保持字面量。"
    "不支持图片、脚注、任务列表、单元格内换行。"
)

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
            "注意：若本轮发现某条**已有**记忆的说法过时了，别只是再写一条新的"
            "（新旧并存会让检索打架），改用 memory_update 修正那一条。"
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
        "description": (
            "手动备份长时陈述性记忆到本地 backups 目录（或指定路径）。"
            "同时落一份配对的知识库快照（knowledge_<同一时间戳>.json），memory_restore 会一并恢复。"
        ),
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
        "name": "memory_knowledge_add",
        "description": (
            "把整篇飞书文档收进知识库：按小节切块存下来，之后 memory_prepare / memory_ask "
            "会在 context_pack 里带出相关原文片段。"
            "区分：整篇长期备查用本工具；只想把文档摘成一条待确认记忆用 memory_feishu_bookmark；"
            "只是这一次要看正文用 memory_feishu_read。"
            "记忆里带的飞书链接会自动入库，一般不必手动调。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "memory_knowledge_list",
        "description": "列出知识库里已收录的文档（标题、链接、字数、块数、失败原因）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_knowledge_backfill",
        "description": (
            "全量扫描长时记忆，把里面出现过、但还没入库的飞书文档补录进知识库。"
            "自动入库只在写记忆那一刻触发，启用知识库之前的存量记忆要靠这个补齐。"
            "串行抓取，十几篇要一分钟；先用 dry_run=true 看会抓哪些。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "只列出候选，不抓取"},
                "refresh": {"type": "boolean", "description": "已入库的也重抓一遍"},
                "limit": {"type": "integer", "description": "最多抓几篇；0 或省略为不限"},
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
        "name": "memory_feishu_read",
        "description": (
            "读飞书文档正文纯文本（需已配置并登录飞书）。要看/分析/引用文档内容就用这个。"
            "超长文档按 max_chars 截断，返回 next_offset，用同一 url 带 offset 接着读。"
            "区分：要正文用本工具；只想确认「是哪一篇、多少块」用 memory_feishu_preview；"
            "想把文档存成记忆候选用 memory_feishu_bookmark。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
                "max_chars": {
                    "type": "integer",
                    "description": "单次返回正文上限字符数，默认 30000",
                    "default": 30000,
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几个字符开始读，配合 next_offset 续读，默认 0",
                    "default": 0,
                },
                "include_widgets": {
                    "type": "boolean",
                    "description": (
                        "默认 true：额外把画板（流程图/架构图）读成文字附在正文后。"
                        "画板内容不在正文里，关掉就完全看不到它存在。"
                        "只想快速取正文、且确认文档没有画板时可设 false，省两次请求。"
                    ),
                    "default": True,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "memory_feishu_preview",
        "description": (
            "只读查看飞书文档的标题与当前正文块数（需已配置并登录飞书）。**不返回正文**，"
            "要正文请用 memory_feishu_read。"
            "改正文前先用它确认「改的是哪一篇、会动多少内容」，再拿给用户确认。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "memory_feishu_list_comments",
        "description": (
            "只读：拉飞书文档的全部评论（含别人在客户端里加的局部评论，看 is_whole / quote 区分）。"
            "适合看「评审意见都提了什么」。需已开通 docs:document.comment:read。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
                "max_comments": {
                    "type": "integer",
                    "description": "最多返回多少条评论，默认 200",
                    "default": 200,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "memory_feishu_comment",
        "description": (
            "给飞书文档加评论（写操作，会通知文档协作者）。三种模式："
            "传 anchor_text 或 block_id = **局部评论（划词评论）**，锚定到具体段落、"
            "在正文旁边显示并带引用；传 comment_id = 回复已有评论；"
            "都不传 = 全文评论，显示在文档底部。"
            "**审阅/逐条指出问题时优先用 anchor_text 做局部评论**，别把多个问题"
            "合并成一条全文评论。anchor_text 命中多个段落会报错，这时换更独特的"
            "片段或改用 block_id。"
            "【硬性约定】必须先在对话里说明「评论哪一篇、评论在哪段、评论什么内容」，"
            "取得用户本轮明确同意后才能传 confirmed=true；上一轮批准过不算本次的确认。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
                "text": {"type": "string", "description": "评论内容（纯文本）"},
                "anchor_text": {
                    "type": "string",
                    "description": (
                        "要评论的原文片段（取一小段连续且独特的文字即可），"
                        "命中的那个段落会成为评论锚点"
                    ),
                },
                "block_id": {
                    "type": "string",
                    "description": "已知块 ID 时直接锚定；与 anchor_text 二选一",
                },
                "comment_id": {
                    "type": "string",
                    "description": "填则视为回复该条评论；不能与 anchor_text/block_id 同时用",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "必须为 true 才执行；仅在用户本轮明确同意后传",
                    "default": False,
                },
            },
            "required": ["url", "text", "confirmed"],
        },
    },
    {
        "name": "memory_feishu_create_doc",
        "description": (
            "在本人云空间新建飞书文档，可带 Markdown 正文（写操作）。"
            "【硬性约定】必须先在对话里说明「要建什么标题、建在哪、正文大意」，"
            "取得用户本轮明确同意后才能传 confirmed=true；用户没说就不要自行调用。"
            "上一轮批准过不算本次的确认。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "文档标题"},
                "content": {
                    "type": "string",
                    "description": f"正文。{_MARKDOWN_HELP}",
                },
                "folder_token": {
                    "type": "string",
                    "description": "目标文件夹 token；省略则建在云空间根目录",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "必须为 true 才执行；仅在用户本轮明确同意后传",
                    "default": False,
                },
            },
            "required": ["title", "confirmed"],
        },
    },
    {
        "name": "memory_feishu_edit_body",
        "description": (
            "改飞书文档正文（写操作）。mode=append 追加到末尾；mode=replace 删掉原正文再写入。"
            "【硬性约定】必须先用 memory_feishu_preview 确认目标文档，在对话里说明"
            "「改哪一篇、追加还是替换、会删多少块」，取得用户本轮明确同意后才能传 confirmed=true。"
            "replace 会真的删除原有块（飞书侧可用历史版本恢复），尤其不可自行发起。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
                "content": {
                    "type": "string",
                    "description": f"新正文。{_MARKDOWN_HELP}",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "replace"],
                    "description": "append=追加到末尾（安全）；replace=删原正文再写（破坏性）",
                    "default": "append",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "必须为 true 才执行；仅在用户本轮明确同意后传",
                    "default": False,
                },
            },
            "required": ["url", "content", "confirmed"],
        },
    },
    {
        "name": "memory_feishu_set_title",
        "description": (
            "改飞书 wiki 节点标题（写操作）。只支持 wiki 链接，docx 直链没有 space_id/node_token。"
            "【硬性约定】必须先在对话里说明「改哪一篇、从什么改成什么」，"
            "取得用户本轮明确同意后才能传 confirmed=true。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 链接"},
                "title": {"type": "string", "description": "新标题"},
                "confirmed": {
                    "type": "boolean",
                    "description": "必须为 true 才执行；仅在用户本轮明确同意后传",
                    "default": False,
                },
            },
            "required": ["url", "title", "confirmed"],
        },
    },
    {
        "name": "memory_feishu_create_board",
        "description": (
            "新建飞书画板，可顺手把一串步骤画成流程图（写操作）。"
            "飞书没有独立的画板文件：画板永远是某篇文档里的一个块，所以要么给 url "
            "把画板插进已有文档，要么给 title 先建一篇新文档再插。"
            "【硬性约定】必须先在对话里说明「插到哪篇文档、画什么内容」，"
            "取得用户本轮明确同意后才能传 confirmed=true；上一轮批准过不算本次的确认。"
            "返回里的 whiteboard_id 可交给 memory_feishu_board_draw 继续画。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "把画板插进这篇文档（wiki 或 docx 链接）；与 title 二选一",
                },
                "title": {
                    "type": "string",
                    "description": "没有 url 时新建文档的标题，画板插在这篇新文档里",
                },
                "folder_token": {
                    "type": "string",
                    "description": "新建文档时的目标文件夹 token；省略则建在云空间根目录",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "流程图的各个方框文字，按顺序连线；省略则只建一个空画板",
                },
                "edge_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "连线上的文字，第 i 个标在第 i 与 i+1 个方框之间；可省略",
                },
                "direction": {
                    "type": "string",
                    "enum": ["down", "right"],
                    "description": "流程走向：down=从上往下，right=从左往右",
                    "default": "down",
                },
                "shape": {
                    "type": "string",
                    "enum": ["round_rect", "rect", "ellipse", "diamond", "parallelogram"],
                    "description": "方框图形",
                    "default": "round_rect",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "必须为 true 才执行；仅在用户本轮明确同意后传",
                    "default": False,
                },
            },
            "required": ["confirmed"],
        },
    },
    {
        "name": "memory_feishu_board_draw",
        "description": (
            "往已有飞书画板里画一串流程图节点（写操作，内容是追加的，不会清空原有图形）。"
            "whiteboard_id 在界面上看不见：给文档链接调 memory_feishu_list_boards 可以列出来。"
            "【硬性约定】必须先在对话里说明「往哪个画板画什么」，"
            "取得用户本轮明确同意后才能传 confirmed=true。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "whiteboard_id": {"type": "string", "description": "画板 id"},
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "流程图的各个方框文字，按顺序连线",
                },
                "edge_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "连线上的文字；可省略",
                },
                "direction": {
                    "type": "string",
                    "enum": ["down", "right"],
                    "default": "down",
                },
                "shape": {
                    "type": "string",
                    "enum": ["round_rect", "rect", "ellipse", "diamond", "parallelogram"],
                    "default": "round_rect",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "必须为 true 才执行；仅在用户本轮明确同意后传",
                    "default": False,
                },
            },
            "required": ["whiteboard_id", "steps", "confirmed"],
        },
    },
    {
        "name": "memory_feishu_list_boards",
        "description": (
            "列出一篇飞书文档里的所有画板及其 whiteboard_id（只读，无需确认）。"
            "whiteboard_id 在飞书界面上看不到，要往已有画板里画东西就得先用它查。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "飞书 wiki 或 docx 链接"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "memory_restore",
        "description": (
            "从备份恢复长时记忆（覆盖当前）。不传 path 则恢复最新一份备份。"
            "同一时间戳的知识库快照会一并恢复；老备份没有那份快照时知识库不动。"
        ),
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
        "name": "memory_update",
        "description": (
            "原地修正一条已有记忆，用于**记忆已过时/被现状推翻**的情况："
            "取值变了、规范改了、方案被替换、结论被证伪。"
            "id 从 memory_prepare / memory_ask 的 references 或 context_pack 里的 id= 取。"
            "省略的字段沿用原值（只改答案就只传 answer）。"
            "定位不到原条目会报错而不是新建——同一件事留着新旧两种说法比不更新更糟。"
            "整条已无价值就用 memory_delete；只是补充新增结论请用 memory_remember。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "记忆 id（首选，来自 references / context_pack 的 id=）",
                },
                "question": {
                    "type": "string",
                    "description": "没有 id 时用问法或别名精确定位",
                },
                "answer": {
                    "type": "string",
                    "description": (
                        "修正后的完整结论正文（会整段替换原答案）。"
                        "建议保留「旧值已废弃」这类线索，别让读者以为从来如此。"
                        "**修订说明要写在开头**：context_pack 只带答案前 800 字，"
                        "写在末尾的更正会被截掉，检索方只看到旧结论。"
                    ),
                },
                "new_question": {
                    "type": "string",
                    "description": "要改问法时才传；省略则保持原问法不动",
                },
                "scene": {"type": "string", "description": "场景，省略沿用原值"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签，省略沿用原值（传了则整体替换）",
                },
                "kind": {
                    "type": "string",
                    "description": "qa/command/path/env/pitfall/decision，省略沿用原值",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么要改（写进回执便于用户核对，不入库）",
                },
            },
            "required": ["answer"],
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "删除单条已记住的问答。优先传 memory_id；否则传 question 精确匹配。"
            "记忆整条都已失效（讲的东西不存在了）时用它；只是内容过时应优先 memory_update。"
        ),
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
        "若某条与现状矛盾，用它的 id 调 memory_update 修正或 memory_delete 删除，"
        "别留着误导后续检索。"
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
        # id 必须给：hook 投递的是纯文本，没有 id 就没法回头修这条
        meta = f"id={r.get('id') or ''} score={score_s}{tag_s}"
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
        # MCP 这条路是只读的：memory_ask("记一下这个") 不该悄悄写一条库
        result = sb.ask_local(query, allow_commands=False)
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


def _feishu_ref(url: str):
    """从入参里取第一个飞书链接；取不到返回 None。"""
    from core.feishu import extract_feishu_urls

    refs = extract_feishu_urls(url or "")
    return refs[0] if refs else None


def _call_board_tool(
    sb: MemorySandbox,
    name: str,
    args: Dict[str, Any],
    cfg: Any,
    config_path: str,
    remember,
    need_confirm,
) -> Dict[str, Any]:
    """
    画板的两个写工具。单独抽出来是因为它们的参数与文档写操作差得远
    （steps / direction / shape），塞进 _call_feishu_tool 会把那个函数撑成一团。
    """
    from core.feishu_board import create_board, draw_board_flow

    if not bool(args.get("confirmed")):
        return need_confirm()
    steps = [str(s) for s in (args.get("steps") or [])]
    labels = [str(s) for s in (args.get("edge_labels") or [])]
    common = {
        "direction": (args.get("direction") or "down").strip() or "down",
        "shape": (args.get("shape") or "round_rect").strip() or "round_rect",
        "edge_labels": labels,
        "config_path": config_path,
        "confirmed": True,
    }

    if name == "memory_feishu_board_draw":
        res = draw_board_flow(
            cfg, (args.get("whiteboard_id") or "").strip(), steps, **common
        )
        payload = {
            "ok": res.ok,
            "whiteboard_id": res.whiteboard_id,
            "nodes_written": res.nodes_written,
            "error": res.error,
        }
        # 这条不自动落库：往已有画板追加图形没有稳定的「哪篇文档」可挂，
        # 硬记会生成一条只有 whiteboard_id 的孤儿记忆。结论请自己 memory_remember
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
        )

    res = create_board(
        cfg,
        url=(args.get("url") or "").strip(),
        title=(args.get("title") or "").strip(),
        folder_token=(args.get("folder_token") or "").strip(),
        steps=steps,
        **common,
    )
    payload = {
        "ok": res.ok,
        "whiteboard_id": res.whiteboard_id,
        "block_id": res.block_id,
        "document_id": res.document_id,
        "url": res.url,
        "title": res.title,
        "nodes_written": res.nodes_written,
        "error": res.error,
    }
    payload["remembered"] = remember(
        action="board",
        url=res.url,
        title=res.title,
        document_id=res.document_id,
        content="\n".join(f"- {s}" for s in steps),
        blocks_written=res.nodes_written,
        ok=res.ok,
        error=res.error,
    )
    return _tool_result(
        json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
    )


def _call_feishu_tool(
    sb: MemorySandbox, name: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    """
    飞书读写工具。写操作一律要求显式 confirmed=true。

    这里的门禁和 CLI 是同一层（core.feishu 的写函数默认拒绝），MCP 只是再挡一次，
    好让「未确认」直接以工具错误的形式回给调用方，而不是发出请求后才失败。
    """
    from core.feishu import (
        create_docx_comment,
        create_docx_document,
        fetch_feishu_document,
        list_docx_comments,
        preview_docx_body,
        update_docx_body,
        update_wiki_node_title,
    )

    cfg = _fresh_feishu_cfg(sb)
    config_path = str(default_config_path())

    def _remember(**kw: Any) -> str:
        """
        落库失败绝不能盖掉「文档已经写成功」这件事：否则调用方看到 isError
        可能重试，于是又建一篇重复文档。出错就把原因塞回 payload。
        """
        try:
            return sb.remember_feishu_write(**kw) or "未落库（飞书侧无实际改动）"
        except Exception as e:  # noqa: BLE001
            return f"落库失败（飞书侧改动已生效，请手动 memory_remember）：{e}"

    def _need_confirm() -> Dict[str, Any]:
        return _tool_result(
            "未确认：改动飞书文档需用户本轮明确同意。请先在对话里说明要改哪一篇、"
            "怎么改，得到用户同意后再带 confirmed=true 调用。",
            is_error=True,
        )

    if name == "memory_feishu_read":
        ref = _feishu_ref(args.get("url") or "")
        if ref is None:
            return _tool_result("不是有效的飞书文档链接", is_error=True)
        want_widgets = args.get("include_widgets")
        res = fetch_feishu_document(
            cfg,
            ref,
            config_path=config_path,
            include_widgets=True if want_widgets is None else bool(want_widgets),
        )
        if not res.ok:
            return _tool_result(
                json.dumps(
                    {"ok": False, "url": res.url, "error": res.error},
                    ensure_ascii=False,
                    indent=2,
                ),
                is_error=True,
            )
        content = res.content or ""
        total = len(content)
        try:
            offset = max(0, int(args.get("offset") or 0))
            max_chars = int(args.get("max_chars") or 30000)
        except (TypeError, ValueError):
            return _tool_result("offset / max_chars 必须是整数", is_error=True)
        max_chars = max(1, max_chars)
        chunk = content[offset : offset + max_chars]
        end = offset + len(chunk)
        payload = {
            "ok": True,
            "url": res.url,
            "title": res.title,
            "document_id": res.document_id,
            "total_chars": total,
            "offset": offset,
            "returned_chars": len(chunk),
            "truncated": end < total,
            "next_offset": end if end < total else None,
            "content": chunk,
        }
        return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

    if name == "memory_feishu_preview":
        ref = _feishu_ref(args.get("url") or "")
        if ref is None:
            return _tool_result("不是有效的飞书文档链接", is_error=True)
        pre = preview_docx_body(cfg, ref, config_path=config_path)
        payload = {
            "ok": pre.ok,
            "url": pre.url,
            "title": pre.title,
            "document_id": pre.document_id,
            "block_count": pre.block_count,
            "error": pre.error,
        }
        return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

    if name == "memory_feishu_list_comments":
        ref = _feishu_ref(args.get("url") or "")
        if ref is None:
            return _tool_result("不是有效的飞书文档链接", is_error=True)
        try:
            cap = int(args.get("max_comments") or 200)
        except (TypeError, ValueError):
            return _tool_result("max_comments 必须是整数", is_error=True)
        res = list_docx_comments(cfg, ref, config_path=config_path, max_comments=cap)
        payload = {
            "ok": res.ok,
            "url": res.url,
            "title": res.title,
            "document_id": res.document_id,
            "count": len(res.comments),
            "truncated": res.truncated,
            "error": res.error,
            "comments": [
                {
                    "comment_id": c.comment_id,
                    "user_id": c.user_id,
                    "created_at": c.created_at,
                    "is_whole": c.is_whole,
                    "is_solved": c.is_solved,
                    "quote": c.quote,
                    "replies": c.replies,
                }
                for c in res.comments
            ],
        }
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
        )

    if name == "memory_feishu_comment":
        if not bool(args.get("confirmed")):
            return _need_confirm()
        ref = _feishu_ref(args.get("url") or "")
        if ref is None:
            return _tool_result("不是有效的飞书文档链接", is_error=True)
        res = create_docx_comment(
            cfg,
            ref,
            args.get("text") or "",
            comment_id=(args.get("comment_id") or "").strip(),
            block_id=(args.get("block_id") or "").strip(),
            anchor_text=(args.get("anchor_text") or "").strip(),
            config_path=config_path,
            confirmed=True,
        )
        payload = {
            "ok": res.ok,
            "url": res.url,
            "title": res.title,
            "document_id": res.document_id,
            "comment_id": res.comment_id,
            "replied_to": res.replied_to,
            "reply_id": res.reply_id,
            "block_id": res.block_id,
            "is_whole": not (res.block_id or res.replied_to),
            "error": res.error,
        }
        payload["remembered"] = _remember(
            action="comment",
            url=res.url,
            title=res.title,
            document_id=res.document_id,
            content=args.get("text") or "",
            ok=res.ok,
            error=res.error,
        )
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
        )

    if name == "memory_feishu_create_doc":
        if not bool(args.get("confirmed")):
            return _need_confirm()
        res = create_docx_document(
            cfg,
            (args.get("title") or "").strip(),
            content=args.get("content") or "",
            folder_token=(args.get("folder_token") or "").strip(),
            config_path=config_path,
            confirmed=True,
        )
        payload = {
            "ok": res.ok,
            "title": res.title,
            "url": res.url,
            "document_id": res.document_id,
            "blocks_written": res.blocks_written,
            "error": res.error,
        }
        payload["remembered"] = _remember(
            action="create",
            url=res.url,
            title=res.title,
            document_id=res.document_id,
            content=args.get("content") or "",
            blocks_written=res.blocks_written,
            ok=res.ok,
            error=res.error,
        )
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
        )

    if name in ("memory_feishu_create_board", "memory_feishu_board_draw"):
        return _call_board_tool(sb, name, args, cfg, config_path, _remember, _need_confirm)

    if name == "memory_feishu_list_boards":
        from core.feishu_board import list_document_boards

        boards, err = list_document_boards(
            cfg, args.get("url") or "", config_path=config_path
        )
        payload = {"ok": not err, "boards": boards, "count": len(boards), "error": err}
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=bool(err)
        )

    if name == "memory_feishu_edit_body":
        if not bool(args.get("confirmed")):
            return _need_confirm()
        ref = _feishu_ref(args.get("url") or "")
        if ref is None:
            return _tool_result("不是有效的飞书文档链接", is_error=True)
        res = update_docx_body(
            cfg,
            ref,
            args.get("content") or "",
            mode=(args.get("mode") or "append").strip() or "append",
            config_path=config_path,
            confirmed=True,
        )
        payload = {
            "ok": res.ok,
            "url": res.url,
            "title": res.title,
            "mode": res.mode,
            "document_id": res.document_id,
            "blocks_written": res.blocks_written,
            "blocks_deleted": res.blocks_deleted,
            "error": res.error,
        }
        payload["remembered"] = _remember(
            action=res.mode or (args.get("mode") or "append"),
            url=res.url,
            title=res.title,
            document_id=res.document_id,
            content=args.get("content") or "",
            blocks_written=res.blocks_written,
            blocks_deleted=res.blocks_deleted,
            ok=res.ok,
            error=res.error,
        )
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
        )

    if name == "memory_feishu_set_title":
        if not bool(args.get("confirmed")):
            return _need_confirm()
        ref = _feishu_ref(args.get("url") or "")
        if ref is None:
            return _tool_result("不是有效的飞书文档链接", is_error=True)
        res = update_wiki_node_title(
            cfg,
            ref,
            (args.get("title") or "").strip(),
            config_path=config_path,
            confirmed=True,
        )
        payload = {
            "ok": res.ok,
            "url": res.url,
            "old_title": res.old_title,
            "new_title": res.new_title,
            "error": res.error,
        }
        payload["remembered"] = _remember(
            action="title",
            url=res.url,
            title=res.new_title,
            old_title=res.old_title,
            ok=res.ok,
            error=res.error,
        )
        return _tool_result(
            json.dumps(payload, ensure_ascii=False, indent=2), is_error=not res.ok
        )

    return _tool_result(f"未知工具: {name}", is_error=True)


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

        if name == "memory_knowledge_add":
            url = (args.get("url") or "").strip()
            if not url:
                return _tool_result("url 不能为空", is_error=True)
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            res = sb.add_knowledge(url, tags=tags)
            if not res.get("ok"):
                return _tool_result(res.get("error") or "入库失败", is_error=True)
            doc = res.get("doc") or {}
            if res.get("skipped"):
                return _tool_result(f"知识库里已有《{doc.get('title')}》，跳过重复抓取")
            return _tool_result(
                f"已收进知识库：《{doc.get('title')}》"
                f"（{doc.get('char_count', 0)} 字 / {doc.get('chunk_count', 0)} 块）"
            )

        if name == "memory_knowledge_list":
            payload = {
                "stats": sb.knowledge.stats(),
                "docs": [
                    {
                        "id": d.get("id"),
                        "title": d.get("title"),
                        "url": d.get("url"),
                        "char_count": d.get("char_count"),
                        "chunk_count": d.get("chunk_count"),
                        "last_error": d.get("last_error"),
                    }
                    for d in sb.knowledge.list_docs()
                ],
            }
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name == "memory_knowledge_backfill":
            refresh = bool(args.get("refresh"))
            try:
                limit = int(args.get("limit") or 0)
            except (TypeError, ValueError):
                limit = 0
            if bool(args.get("dry_run")):
                pending = sb.scan_memory_links(refresh=refresh)
                if limit > 0:
                    pending = pending[:limit]
                return _tool_result(
                    json.dumps(
                        {"candidates": len(pending), "docs": pending},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            res = sb.backfill_knowledge(refresh=refresh, limit=limit)
            return _tool_result(
                f"扫描 {res['scanned']} 条记忆，候选 {res['candidates']} 篇，"
                f"成功 {len(res['done'])} 篇，失败 {len(res['failed'])} 篇\n"
                + json.dumps(
                    {"done": res["done"], "failed": res["failed"]},
                    ensure_ascii=False,
                    indent=2,
                )
            )

        if name == "memory_feishu_bookmark":
            text = (args.get("text") or "").strip()
            if not text:
                return _tool_result("text 不能为空", is_error=True)
            payload = sb.bookmark_feishu(text)
            return _tool_result(json.dumps(payload, ensure_ascii=False, indent=2))

        if name in {
            "memory_feishu_read",
            "memory_feishu_preview",
            "memory_feishu_list_comments",
            "memory_feishu_comment",
            "memory_feishu_create_doc",
            "memory_feishu_edit_body",
            "memory_feishu_set_title",
            "memory_feishu_create_board",
            "memory_feishu_board_draw",
            "memory_feishu_list_boards",
        }:
            return _call_feishu_tool(sb, name, args)

        if name == "memory_restore":
            if not bool(args.get("confirm")):
                return _tool_result(
                    "恢复会覆盖当前长时记忆。请再次调用并传 confirm=true。",
                    is_error=True,
                )
            path = (args.get("path") or "").strip() or None
            return _tool_result(sb.restore_long_term(path))

        if name == "memory_update":
            memory_id = (args.get("memory_id") or "").strip()
            question = (args.get("question") or "").strip()
            if not memory_id and not question:
                return _tool_result(
                    "请提供 memory_id（首选）或 question 以定位要修正的记忆", is_error=True
                )
            tags = args.get("tags")
            before = sb.find_memory(memory_id=memory_id, question=question)
            msg = sb.update_memory(
                memory_id=memory_id,
                question=question,
                answer=args.get("answer") or "",
                new_question=args.get("new_question"),
                scene=(args.get("scene") or "").strip() or None,
                tags=[str(t) for t in tags] if isinstance(tags, list) else None,
                kind=(args.get("kind") or "").strip() or None,
            )
            failed = msg.startswith(("未找到", "answer 不能为空"))
            if failed:
                return _tool_result(msg, is_error=True)
            reason = (args.get("reason") or "").strip()
            # 回执带上旧答案摘要：用户能直接看出改掉了什么
            old = (before.answer or "").strip() if before else ""
            parts = [msg]
            if reason:
                parts.append(f"原因：{reason}")
            if old:
                parts.append(f"原答案（前 200 字）：{old[:200]}")
            return _tool_result("\n".join(parts))

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
