#!/usr/bin/env python3
"""记忆沙箱 CLI：子命令 + 交互模式，与 MCP/Web 共用用户记忆目录。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# 保证以脚本方式运行时可导入 core
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import MemorySandbox, bot_process, cursor_hooks
from core.cli_ui import CliUi
from core.config import agent_ui_mode_from_config, load_config
from core.paths import default_config_path, default_persist_dir
from core.utils import assemble_long_term_query, clean_text


def _format_hooks_status(st: "cursor_hooks.HooksStatus") -> str:
    if st.error:
        head = f"异常：{st.error}"
    elif not st.installed:
        head = "未安装（AI 不会被强制先查记忆、也不会被追问落库）"
    elif not st.up_to_date:
        head = "已安装，但脚本是旧版，建议重新执行 hooks-install"
    else:
        head = "已安装且是最新版"

    lines = [
        head,
        f"- 配置文件：{st.hooks_json}",
        f"- 脚本目录：{st.hooks_dir}",
        f"- 解释器：{st.python}",
    ]
    if st.installed_at:
        lines.append(f"- 安装时间：{st.installed_at}")
    if st.missing_scripts:
        lines.append(f"- 缺少脚本：{', '.join(st.missing_scripts)}")
    if st.stale_scripts:
        lines.append(f"- 待更新脚本：{', '.join(st.stale_scripts)}")
    if st.missing_events:
        lines.append(f"- 未挂载事件：{', '.join(st.missing_events)}")
    lines.append(f"- 你自己的其它 hook：{st.foreign_entries} 条（安装/卸载都不会动）")
    return "\n".join(lines)


def _format_bot_status(st: "bot_process.BotStatus") -> str:
    if st.running:
        where = "" if st.owned else "，不是 BloomBox 起的"
        head = f"运行中（PID {st.pid}{where}）"
    elif not st.available:
        head = f"未运行：找不到 {st.script}"
    elif not st.sdk_installed:
        head = "未运行：缺 lark-oapi（pip install lark-oapi）"
    elif not st.configured:
        head = "未运行：还没配 feishu.app_id / app_secret"
    else:
        head = "未运行"

    lines = [head]
    if st.started_at:
        lines.append(f"- 启动时间：{st.started_at}")
    lines.append(f"- 白名单：{st.allow_count} 人")
    lines.append(f"- 文档评论机器人：{'开' if st.doc_bot_enabled else '关'}")
    lines.append(f"- 日志：{st.log}")
    if st.error:
        lines.append(f"- 读配置：{st.error}")
    return "\n".join(lines)


def build_sandbox(config_path: Optional[str] = None, use_user_memory: bool = True) -> MemorySandbox:
    """默认与 MCP/Web 共用 Application Support 记忆；可用 --project-memory 改用项目 data/。"""
    cfg_path = config_path or str(default_config_path())
    cfg = load_config(cfg_path)
    if use_user_memory:
        cfg.long_term.persist_dir = str(default_persist_dir())
    return MemorySandbox(config=cfg, config_path=cfg_path)


def seed_dev_memories(sandbox: MemorySandbox) -> None:
    samples = [
        ("如何启动本地前端", "在项目根目录执行 pnpm install && pnpm start，注意检查 .npmrc 私源配置。"),
        ("agency 项目怎么跑", "进入 live_web_agency，执行 pnpm install，再 pnpm start；e2e 用 agency-e2e。"),
        ("切换开发环境要注意什么", "确认当前 Node/pnpm 版本、hosts/代理、环境变量（.env）以及对应业务的 mock 开关。"),
        ("记忆沙箱怎么减少 token", "优先把高频问答用「记住：问 => 答」写入长时记忆；重复问题会直接命中沙箱，不走大模型。"),
        ("git 提交规范", "使用简洁祈使句说明 why；不要自动 push；不要改 git config。"),
    ]
    for q, a in samples:
        sandbox.remember(q, a, scene="dev")
    sandbox.working.set_scene("dev")
    print(f"已写入 {len(samples)} 条开发场景记忆，当前场景: dev")


def _print_result(result, as_json: bool, ui: Optional[CliUi] = None) -> None:
    if ui is not None and not as_json:
        ui.print_result(result, as_json=False)
        return
    if as_json:
        print(
            json.dumps(
                {
                    "answer": result.answer,
                    "source": result.source,
                    "meta": result.meta,
                    "hit_local": result.source not in ("miss", "llm", "sensory_reject"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if result.answer:
        print(result.answer)
    else:
        print("(无本地命中)" if result.source == "miss" else "")
    print(f"(source={result.source})", file=sys.stderr)


def _feishu_content_arg(args) -> Optional[str]:
    """取飞书正文：--content-file 优先于 --content；读失败返回 None。"""
    if args.content_file:
        try:
            with open(args.content_file, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            print(f"读正文文件失败：{e}", file=sys.stderr)
            return None
    return args.content or ""


def _remember_feishu_write(sb: MemorySandbox, **kw) -> None:
    """
    把飞书写操作落库。飞书侧没动过时静默跳过（由 build_write_memory 判定）。

    落库失败只提示，不改命令退出码：文档已经改成功了，不该因为记账失败
    让调用方以为写操作没生效、跑去重试。
    """
    try:
        msg = sb.remember_feishu_write(**kw)
    except Exception as e:  # noqa: BLE001
        print(f"（落库失败，飞书侧改动已生效，请手动 remember：{e}）", file=sys.stderr)
        return
    if msg:
        print(msg)


def _feishu_board_cmd(sb: MemorySandbox, cmd: str, args) -> int:
    """画板三条命令：列画板（只读）、建画板、往已有画板画。"""
    from core.feishu_board import create_board, draw_board_flow, list_document_boards

    config_path = args.config or str(default_config_path())

    if cmd == "feishu-boards":
        boards, err = list_document_boards(sb.config.feishu, args.url, config_path=config_path)
        if err:
            print(f"读取失败：{err}", file=sys.stderr)
            return 2
        if not boards:
            print("这篇文档里没有画板")
            return 0
        for i, b in enumerate(boards, start=1):
            print(f"{i}. whiteboard_id={b['whiteboard_id']}  block_id={b['block_id']}")
        return 0

    steps = [s for s in (args.steps or []) if s.strip()]
    labels = list(args.labels or [])

    if cmd == "feishu-board-draw":
        if not steps:
            print("至少要有一个 --step，否则没东西可画", file=sys.stderr)
            return 2
        if not args.yes:
            print(f"将往画板 {args.whiteboard_id} 追加 {len(steps)} 个方框：")
            print("  " + " → ".join(steps))
            if input("确认？(y/N) ").strip().lower() not in {"y", "yes"}:
                print("已取消")
                return 1
        res = draw_board_flow(
            sb.config.feishu,
            args.whiteboard_id,
            steps,
            direction=args.direction,
            shape=args.shape,
            edge_labels=labels,
            config_path=config_path,
            confirmed=True,
        )
        if not res.ok:
            print(f"写入失败：{res.error}", file=sys.stderr)
            return 2
        print(f"已画上 {res.nodes_written} 个节点（含连线）")
        return 0

    if not args.yes:
        where = f"已有文档 {args.url}" if args.url else f"新建文档《{args.title}》"
        print(f"将在{where}里插入一个画板")
        print("  " + (" → ".join(steps) if steps else "（空画板，不画内容）"))
        if input("确认？(y/N) ").strip().lower() not in {"y", "yes"}:
            print("已取消")
            return 1
    res = create_board(
        sb.config.feishu,
        url=args.url,
        title=args.title,
        folder_token=args.folder or "",
        steps=steps,
        direction=args.direction,
        shape=args.shape,
        edge_labels=labels,
        config_path=config_path,
        confirmed=True,
    )
    remember_kw = dict(
        action="board",
        url=res.url,
        title=res.title,
        document_id=res.document_id,
        content="\n".join(f"- {s}" for s in steps),
        blocks_written=res.nodes_written,
        ok=res.ok,
        error=res.error,
    )
    if not res.ok:
        print(f"建画板失败：{res.error}", file=sys.stderr)
        if res.whiteboard_id:
            # 画板已经建出来了（只是没画上内容），id 必须给出去，否则用户既
            # 找不到它也不知道有个空画板要清理
            print(f"画板已存在：whiteboard_id={res.whiteboard_id}", file=sys.stderr)
            _remember_feishu_write(sb, **remember_kw)
        return 2
    print(f"已建画板：whiteboard_id={res.whiteboard_id}（画上 {res.nodes_written} 个节点）")
    print(res.url or f"document_id={res.document_id}（配置 feishu.doc_host 可输出链接）")
    _remember_feishu_write(sb, **remember_kw)
    return 0


def interactive(sandbox: MemorySandbox, as_json: bool = False, local_only: bool = False) -> None:
    ui = CliUi()
    mode = "仅本地记忆" if local_only else "本地记忆 → 可选 LLM"
    llm_line = None
    if not local_only and sandbox.llm is not None:
        from core.llm import describe_cursor_llm

        llm_cfg = sandbox.config.llm
        provider = (llm_cfg.provider or "").lower()
        if provider in {"cursor", "cursor_cloud", "cursor-agent"}:
            llm_line = describe_cursor_llm(llm_cfg)
        else:
            llm_line = f"provider={llm_cfg.provider}"
    if as_json:
        print(f"记忆沙箱 CLI（{mode}）JSON 模式", file=sys.stderr)
    else:
        ui.banner(mode=mode, persist_dir=str(sandbox.long_term.persist_dir), llm_line=llm_line)

    while True:
        try:
            raw = input("\n" + (ui.prompt_label() if not as_json else "你> "))
            text = clean_text(raw)
        except (EOFError, KeyboardInterrupt):
            if as_json:
                print("\n再见。", file=sys.stderr)
            else:
                print("", file=sys.stderr)
                ui.bye()
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit", "q"}:
            if as_json:
                print("再见。", file=sys.stderr)
            else:
                ui.bye()
            break
        ui.begin_turn()
        progress = (lambda m: None) if as_json else ui.progress
        if local_only:
            result = sandbox.ask_local(text, on_progress=progress)
        else:
            result = sandbox.chat(text, on_progress=progress)
        _print_result(result, as_json, ui=None if as_json else ui)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory-sandbox",
        description="记忆沙箱命令行：查记忆 / 记住 / 列表 / 备份等（与 MCP、Web 共用用户记忆）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s ask "revenue 怎么本地启动"
  %(prog)s ask --local "PK组件"          # 只查本地，不调沙箱 LLM
  %(prog)s prepare "revenue怎么启动"     # 拼接「记录到长期记忆」后查本地
  %(prog)s remember "问" "答" --scene dev --tag feishu --kind command --fact "pnpm build"
  %(prog)s extract "\$ pnpm build\\nError: missing env"
  %(prog)s list --layer long_term
  %(prog)s status
  %(prog)s backup
  %(prog)s restore --yes
  %(prog)s delete --question "PK组件"
  %(prog)s scene dev
  %(prog)s                    # 进入交互模式
        """,
    )
    p.add_argument("-c", "--config", default=None, help="配置文件路径（默认优先用户 Application Support）")
    p.add_argument(
        "--project-memory",
        action="store_true",
        help="使用项目 data/memory，而不是用户 Application Support 记忆库",
    )
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument(
        "--agent-mode",
        choices=["ask", "plan", "agent"],
        default=None,
        help="本地 Cursor Agent 模式：ask 只读 | plan 规划 | agent 可写（全局，可持久化）",
    )

    sub = p.add_subparsers(dest="cmd")

    # ask
    sp = sub.add_parser("ask", help="提问（默认本地未命中可走沙箱 LLM）")
    sp.add_argument("query", nargs="+", help="问题文本")
    sp.add_argument("--local", action="store_true", help="仅查本地三级记忆，不调沙箱 LLM")
    sp.add_argument("--json", action="store_true", help="JSON 输出")
    sp.add_argument(
        "--agent-mode",
        choices=["ask", "plan", "agent"],
        default=None,
        help="本轮覆盖 Agent 模式（ask|plan|agent）",
    )

    # prepare（对齐 MCP memory_prepare）
    sp = sub.add_parser("prepare", help="拼接「记录到长期记忆」后只查本地记忆")
    sp.add_argument("query", nargs="+", help="问题文本")
    sp.add_argument("--json", action="store_true", help="JSON 输出")

    # remember
    sp = sub.add_parser("remember", help="写入长时记忆")
    sp.add_argument("question", help="问题 / 检索键")
    sp.add_argument("answer", help="答案 / 知识")
    sp.add_argument("--scene", default="dev", help="场景标签，默认 dev")
    sp.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="标签，可重复：--tag feishu --tag frontend",
    )
    sp.add_argument(
        "--kind",
        default="qa",
        choices=["qa", "command", "path", "env", "pitfall", "decision"],
        help="结构化类型，默认 qa",
    )
    sp.add_argument("--fact", default="", help="对应 kind 的结构化值，如命令或路径")

    sp = sub.add_parser("extract", help="从终端/日志文本提炼候选记忆（不写盘）")
    sp.add_argument("text", nargs="?", default="", help="文本；省略则从 stdin 读")
    sp.add_argument("--max", dest="max_n", type=int, default=3, help="最多条数")
    sp.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="建议标签",
    )

    # list
    sp = sub.add_parser("list", help="列出记忆内容")
    sp.add_argument(
        "--layer",
        choices=["working", "long_term", "all", "short", "long"],
        default="long_term",
        help="记忆层，默认 long_term",
    )

    # status
    sub.add_parser("status", help="打印各层统计")

    # backup / restore / pack / archive
    sp = sub.add_parser("backup", help="备份长时记忆（连同知识库快照）")
    sp.add_argument("--dest", default=None, help="备份文件或目录路径")

    sp = sub.add_parser(
        "feishu-subscribe",
        help="按文件订阅飞书文档事件，让这篇文档的评论能推到机器人",
    )
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")

    # knowledge base
    sp = sub.add_parser("knowledge-add", help="把一篇飞书文档收进知识库")
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")

    sub.add_parser("knowledge-list", help="列出知识库里已收录的文档")

    sp = sub.add_parser(
        "knowledge-backfill",
        help="全量扫描长时记忆，把里面的飞书链接补录进知识库",
    )
    sp.add_argument("--dry-run", action="store_true", help="只列出要抓哪些，不真抓")
    sp.add_argument("--refresh", action="store_true", help="已入库的也重抓一遍")
    sp.add_argument("--limit", type=int, default=0, help="最多抓几篇（0 = 不限）")

    sp = sub.add_parser("pack-export", help="导出可分享知识包（无向量、已脱敏）")
    sp.add_argument("--name", default="memory-pack", help="包名")
    sp.add_argument("--dest", default=None, help="输出文件或目录")
    sp.add_argument("--description", default="", help="说明")
    sp.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="filter_tags",
        help="按标签过滤，可重复",
    )
    sp.add_argument("--scene", default="", dest="filter_scene", help="按场景过滤")
    sp.add_argument("--limit", type=int, default=500)

    sp = sub.add_parser("pack-import", help="导入知识包（默认合并）")
    sp.add_argument("path", help="知识包 JSON 路径")
    sp.add_argument("--replace", action="store_true", help="覆盖导入（先清空）")
    sp.add_argument("--yes", action="store_true", help="覆盖时跳过确认")

    sp = sub.add_parser("archive", help="把很久没用的记忆挪到归档")
    sp.add_argument("--days", type=float, default=None, help="超过多少天未更新")
    sp.add_argument("--min-hits", type=int, default=None, help="命中次数上限")
    sp.add_argument("--yes", action="store_true", help="确认执行")

    sub.add_parser("pack-list", help="列出本机已导出的知识包")

    sp = sub.add_parser("git-check", help="对照 Git 变更，找出可能过时的记忆")
    sp.add_argument("--cwd", default=None, help="项目目录")
    sp.add_argument("--since", default="HEAD~20", help="对比起点，默认 HEAD~20")
    sp.add_argument("--limit", type=int, default=8)

    sp = sub.add_parser("review-suggest", help="从近期 commit 提示可沉淀的协作习惯")
    sp.add_argument("--cwd", default=None, help="项目目录")
    sp.add_argument("--max", dest="max_hints", type=int, default=3)

    sp = sub.add_parser("feishu-bookmark", help="把飞书链接拉成待确认记忆候选")
    sp.add_argument("text", nargs="+", help="含飞书链接的文本")

    sp = sub.add_parser("feishu-read", help="读飞书文档正文（只读）")
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")
    sp.add_argument(
        "--no-widgets",
        action="store_true",
        help="不读画板等组件，只取正文（默认会把画板读成文字附在末尾）",
    )

    sp = sub.add_parser("feishu-set-title", help="改飞书 wiki 节点标题（写操作）")
    sp.add_argument("url", help="飞书 wiki 链接")
    sp.add_argument("title", help="新标题")
    sp.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅限本人执行，AI 不得自行使用",
    )

    sp = sub.add_parser("feishu-create-doc", help="在云空间新建飞书文档（写操作）")
    sp.add_argument("title", help="文档标题")
    sp.add_argument(
        "--content",
        default=None,
        help="正文（Markdown 子集：标题/列表/代码块/引用/表格 + 行内粗体斜体链接）",
    )
    sp.add_argument("--content-file", default=None, help="从文件读正文，优先于 --content")
    sp.add_argument("--folder", default="", help="目标文件夹 token；省略则建在云空间根目录")
    sp.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅限本人执行，AI 不得自行使用",
    )

    sp = sub.add_parser(
        "feishu-create-board", help="新建飞书画板，可顺手画流程图（写操作）"
    )
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", default="", help="把画板插进这篇已有文档")
    g.add_argument("--title", default="", help="先建一篇这个标题的文档，画板插在里面")
    sp.add_argument("--folder", default="", help="新建文档时的目标文件夹 token")
    sp.add_argument(
        "--step",
        action="append",
        default=[],
        dest="steps",
        help="流程图的一个方框，按出现顺序连线；可重复传。不传则只建空画板",
    )
    sp.add_argument(
        "--label",
        action="append",
        default=[],
        dest="labels",
        help="连线上的文字，第 i 个标在第 i 与 i+1 个方框之间；可重复传",
    )
    sp.add_argument(
        "--direction", default="down", choices=["down", "right"], help="流程走向"
    )
    sp.add_argument(
        "--shape",
        default="round_rect",
        choices=["round_rect", "rect", "ellipse", "diamond", "parallelogram"],
        help="方框图形",
    )
    sp.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅限本人执行，AI 不得自行使用",
    )

    sp = sub.add_parser("feishu-board-draw", help="往已有飞书画板里画流程图（写操作）")
    sp.add_argument("whiteboard_id", help="画板 id，用 feishu-boards 查")
    sp.add_argument(
        "--step", action="append", default=[], dest="steps", help="一个方框；可重复传"
    )
    sp.add_argument(
        "--label", action="append", default=[], dest="labels", help="连线文字；可重复传"
    )
    sp.add_argument(
        "--direction", default="down", choices=["down", "right"], help="流程走向"
    )
    sp.add_argument(
        "--shape",
        default="round_rect",
        choices=["round_rect", "rect", "ellipse", "diamond", "parallelogram"],
        help="方框图形",
    )
    sp.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅限本人执行，AI 不得自行使用",
    )

    sp = sub.add_parser("feishu-boards", help="列出文档里的画板与 whiteboard_id（只读）")
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")

    sp = sub.add_parser("feishu-edit-body", help="改飞书文档正文（写操作）")
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--append", action="store_true", help="追加到正文末尾")
    g.add_argument("--replace", action="store_true", help="删掉原正文再写入（破坏性）")
    sp.add_argument(
        "--content",
        default=None,
        help="正文（Markdown 子集：标题/列表/代码块/引用/表格 + 行内粗体斜体链接）",
    )
    sp.add_argument("--content-file", default=None, help="从文件读正文，优先于 --content")
    sp.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅限本人执行，AI 不得自行使用",
    )

    sp = sub.add_parser("feishu-comments", help="列出飞书文档的评论（只读）")
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")
    sp.add_argument("--max", type=int, default=200, help="最多列出多少条，默认 200")
    sp.add_argument("--json", action="store_true", help="输出 JSON")

    sp = sub.add_parser("feishu-comment", help="给飞书文档加评论（写操作）")
    sp.add_argument("url", help="飞书 wiki 或 docx 链接")
    sp.add_argument("text", help="评论内容")
    sp.add_argument(
        "--reply-to",
        default="",
        help="回复某条评论的 comment_id；省略则新建评论",
    )
    sp.add_argument(
        "--on",
        dest="anchor_text",
        default="",
        help="锚定到含这段文字的块，做局部评论（划词评论）；命中多块会报错",
    )
    sp.add_argument(
        "--block-id",
        dest="block_id",
        default="",
        help="已知块 ID 时直接锚定；与 --on 二选一",
    )
    sp.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认；仅限本人执行，AI 不得自行使用",
    )

    sp = sub.add_parser("restore", help="从备份恢复长时记忆与知识库（覆盖）")
    sp.add_argument("--path", default=None, help="备份文件；省略则用最新一份")
    sp.add_argument("--yes", action="store_true", help="跳过确认")

    # forget / delete / clear
    sp = sub.add_parser("forget", help="按关键词遗忘；不传关键词则清空指定层")
    sp.add_argument("keyword", nargs="?", default=None, help="关键词；省略则清空该层")
    sp.add_argument(
        "--layer",
        choices=["all", "sensory", "working", "long_term"],
        default="long_term",
        help="默认 long_term",
    )
    sp.add_argument("--yes", action="store_true", help="清空时跳过确认")

    sp = sub.add_parser("update", help="原地修正一条长时记忆（结论过时了用这个）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", dest="memory_id", help="记忆 id")
    g.add_argument("--question", help="问题原文或核心问法")
    sp.add_argument("answer", help="修正后的完整结论正文（整段替换原答案）")
    sp.add_argument("--new-question", default="", help="要改问法时才传")
    sp.add_argument("--scene", default="", help="省略沿用原值")
    sp.add_argument(
        "--tag",
        action="append",
        default=None,
        dest="tags",
        help="标签，可重复；不传则沿用原值",
    )
    sp.add_argument(
        "--kind",
        default="",
        choices=["", "qa", "command", "path", "env", "pitfall", "decision"],
        help="省略沿用原值",
    )

    sp = sub.add_parser("delete", help="删除单条长时记忆")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", dest="memory_id", help="记忆 id")
    g.add_argument("--question", help="问题原文或核心问法")

    sp = sub.add_parser("clear-long", help="清空全部长时陈述性记忆（需确认）")
    sp.add_argument("--yes", action="store_true", help="确认清空")
    sp.add_argument("--backup-first", action="store_true", help="清空前先备份")

    # scene / seed / reoptimize / agent-mode
    sp = sub.add_parser("scene", help="切换工作记忆场景")
    sp.add_argument("name", help="场景名，如 dev")

    sp = sub.add_parser("agent-mode", help="查看或切换本地 Cursor Agent 模式")
    sp.add_argument(
        "mode",
        nargs="?",
        choices=["ask", "plan", "agent"],
        default=None,
        help="ask 只读 | plan 规划 | agent 可写；省略则显示当前",
    )
    sp.add_argument("--no-persist", action="store_true", help="只改本次进程，不写配置文件")

    # Cursor hook 门禁（先查记忆、结束落库）
    sub.add_parser("hooks-status", help="查看 Cursor hook 门禁的安装状态")

    sp = sub.add_parser(
        "hooks-install", help="把 Cursor hook 门禁装到 ~/.cursor（合并，不覆盖已有 hook）"
    )
    sp.add_argument(
        "--python",
        default="",
        help="指定 hook 用的解释器绝对路径；省略则自动探测",
    )

    sub.add_parser("hooks-uninstall", help="移除 Cursor hook 门禁（保留你其它 hook）")

    # 飞书机器人常驻进程（与 BloomBox 工具栏「飞书机器人」同一套托管）
    sub.add_parser("bot-status", help="查看飞书机器人进程状态")
    sub.add_parser("bot-start", help="后台启动飞书机器人（日志写到文件，退出终端也不掉）")
    sub.add_parser("bot-stop", help="停止飞书机器人")
    sub.add_parser("bot-restart", help="重启飞书机器人")

    sub.add_parser("seed", help="写入一组开发常用种子记忆")

    sub.add_parser("reoptimize", help="对已有长时记忆补别名并刷新向量")

    # interactive
    sp = sub.add_parser("interactive", help="交互模式")
    sp.add_argument("--local", action="store_true", help="交互时仅查本地记忆")
    sp.add_argument("--json", action="store_true", help="JSON 输出")
    sp.add_argument(
        "--agent-mode",
        choices=["ask", "plan", "agent"],
        default=None,
        help="启动时切换 Agent 模式",
    )

    # 兼容旧旗标（无子命令时）
    p.add_argument("-q", "--query", default=None, help=argparse.SUPPRESS)
    p.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--seed", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--backup", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    use_user = not args.project_memory
    sb = build_sandbox(config_path=args.config, use_user_memory=use_user)

    # 全局 --agent-mode（子命令也可各自带）
    global_mode = getattr(args, "agent_mode", None)
    if args.cmd is None and global_mode:
        print(sb.set_agent_mode(global_mode, persist=True), file=sys.stderr)

    # ---- 兼容旧入口 ----
    if not args.cmd:
        if args.seed:
            seed_dev_memories(sb)
            return 0
        if args.backup:
            print(sb.backup_long_term())
            return 0
        if args.status:
            print(json.dumps(sb.status(), ensure_ascii=False, indent=2))
            return 0
        if args.query is not None:
            ui = None if args.json else CliUi()
            if ui:
                ui.begin_turn()
            result = sb.chat(args.query, on_progress=None if args.json else ui.progress)
            _print_result(result, args.json, ui=ui)
            return 0
        interactive(sb, as_json=args.json)
        return 0

    cmd = args.cmd
    as_json = bool(getattr(args, "json", False))

    if cmd == "ask":
        if getattr(args, "agent_mode", None):
            print(sb.set_agent_mode(args.agent_mode, persist=False), file=sys.stderr)
        query = clean_text(" ".join(args.query))
        ui = None if as_json else CliUi()
        if ui:
            ui.begin_turn()
        progress = None if as_json else ui.progress
        if args.local:
            result = sb.ask_local(query, on_progress=progress)
        else:
            result = sb.chat(query, on_progress=progress)
        _print_result(result, as_json, ui=ui)
        return 0

    if cmd == "prepare":
        original = " ".join(args.query).strip()
        assembled = assemble_long_term_query(original)
        result = sb.ask_local(assembled)
        if result.source == "miss" and assembled != original:
            result2 = sb.ask_local(original)
            if result2.source != "miss":
                result = result2
        # 软召回参考：assembled + 原话合并去重
        hits = sb.collect_references(assembled, top_k=5)
        if assembled != original:
            seen = {h.record.id for h in hits}
            for h in sb.collect_references(original, top_k=5):
                if h.record.id not in seen:
                    hits.append(h)
                    seen.add(h.record.id)
            hits = hits[:5]
        context_pack = sb.long_term.format_context_pack(hits)
        if as_json:
            print(
                json.dumps(
                    {
                        "original": original,
                        "assembled": assembled,
                        "answer": result.answer,
                        "source": result.source,
                        "hit_local": result.source
                        not in ("miss", "sensory_reject", "llm"),
                        "references": [h.as_dict() for h in hits],
                        "context_pack": context_pack,
                        "ref_threshold": sb.long_term.soft_threshold(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"assembled: {assembled}", file=sys.stderr)
            _print_result(result, False)
            if context_pack:
                print(context_pack, file=sys.stderr)
        return 0

    if cmd == "remember":
        kind = getattr(args, "kind", None) or "qa"
        fact = (getattr(args, "fact", None) or "").strip()
        facts = {kind: fact} if fact and kind != "qa" else None
        print(
            sb.remember(
                args.question,
                args.answer,
                scene=args.scene,
                tags=getattr(args, "tags", None) or None,
                kind=kind,
                facts=facts,
            )
        )
        return 0

    if cmd == "extract":
        text = (getattr(args, "text", None) or "").strip()
        if not text:
            text = sys.stdin.read()
        payload = sb.extract_candidates(
            text,
            max_n=getattr(args, "max_n", 3) or 3,
            tags=getattr(args, "tags", None) or None,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if cmd == "pack-export":
        print(
            sb.export_pack(
                name=getattr(args, "name", None) or "memory-pack",
                dest=getattr(args, "dest", None),
                description=getattr(args, "description", "") or "",
                filter_tags=getattr(args, "filter_tags", None) or None,
                filter_scene=(getattr(args, "filter_scene", None) or "").strip() or None,
                limit=getattr(args, "limit", 500) or 500,
            )
        )
        return 0

    if cmd == "pack-import":
        merge = not bool(getattr(args, "replace", False))
        if not merge and not getattr(args, "yes", False):
            print("覆盖导入会清空现有长时记忆。请加 --yes 确认。", file=sys.stderr)
            return 2
        print(
            sb.import_pack(
                args.path,
                merge=merge,
                confirm=bool(getattr(args, "yes", False)) or merge,
            )
        )
        return 0

    if cmd == "archive":
        print(
            sb.archive_stale(
                min_hits=getattr(args, "min_hits", None),
                older_than_days=getattr(args, "days", None),
                confirm=bool(getattr(args, "yes", False)),
            )
        )
        return 0

    if cmd == "pack-list":
        print(json.dumps(sb.list_packs(), ensure_ascii=False, indent=2))
        return 0

    if cmd == "git-check":
        print(
            json.dumps(
                sb.check_git_changes(
                    cwd=getattr(args, "cwd", None),
                    since_ref=getattr(args, "since", None) or "HEAD~20",
                    limit=getattr(args, "limit", 8) or 8,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if cmd == "review-suggest":
        print(
            json.dumps(
                sb.suggest_review_notes(
                    cwd=getattr(args, "cwd", None),
                    max_hints=getattr(args, "max_hints", 3) or 3,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if cmd == "feishu-bookmark":
        text = " ".join(getattr(args, "text", []) or []).strip()
        print(json.dumps(sb.bookmark_feishu(text), ensure_ascii=False, indent=2))
        return 0

    if cmd == "feishu-read":
        from core.feishu import extract_feishu_urls, fetch_feishu_document

        refs = extract_feishu_urls(args.url)
        if not refs:
            print("不是有效的飞书文档链接", file=sys.stderr)
            return 2
        res = fetch_feishu_document(
            sb.config.feishu,
            refs[0],
            config_path=args.config or str(default_config_path()),
            include_widgets=not args.no_widgets,
        )
        if not res.ok:
            print(res.error, file=sys.stderr)
            return 2
        print(f"# {res.title}\n")
        print(res.content)
        return 0

    if cmd == "feishu-set-title":
        from core.feishu import extract_feishu_urls, update_wiki_node_title

        refs = extract_feishu_urls(args.url)
        if not refs:
            print("不是有效的飞书文档链接", file=sys.stderr)
            return 2
        ref = refs[0]
        new_title = (args.title or "").strip()
        # 改的是团队共享文档，默认二次确认
        if not args.yes:
            print(f"将把 {ref.url}")
            print(f"的标题改为：{new_title}")
            if input("确认？(y/N) ").strip().lower() not in {"y", "yes"}:
                print("已取消")
                return 1
        res = update_wiki_node_title(
            sb.config.feishu,
            ref,
            new_title,
            config_path=args.config or str(default_config_path()),
            confirmed=True,
        )
        if not res.ok:
            print(f"改标题失败：{res.error}", file=sys.stderr)
            return 2
        print(f"已改标题：{res.old_title or '(原标题未知)'} → {res.new_title}")
        _remember_feishu_write(
            sb,
            action="title",
            url=res.url,
            title=res.new_title,
            old_title=res.old_title,
            ok=True,
        )
        return 0

    if cmd == "feishu-create-doc":
        from core.feishu import create_docx_document, markdown_to_docx_blocks

        content = _feishu_content_arg(args)
        if content is None:
            return 2
        title = (args.title or "").strip()
        # 新建也是飞书侧写操作，默认二次确认
        if not args.yes:
            blocks = markdown_to_docx_blocks(content)
            where = f"文件夹 {args.folder}" if args.folder else "云空间根目录"
            print(f"将在{where}新建文档：{title}")
            print(f"正文：{len(blocks)} 个块（{len(content)} 字）" if blocks else "正文：空")
            if input("确认？(y/N) ").strip().lower() not in {"y", "yes"}:
                print("已取消")
                return 1
        res = create_docx_document(
            sb.config.feishu,
            title,
            content=content,
            folder_token=args.folder or "",
            config_path=args.config or str(default_config_path()),
            confirmed=True,
        )
        remember_kw = dict(
            action="create",
            url=res.url,
            title=res.title or title,
            document_id=res.document_id,
            content=content,
            blocks_written=res.blocks_written,
            ok=res.ok,
            error=res.error,
        )
        if not res.ok:
            print(f"创建失败：{res.error}", file=sys.stderr)
            if res.document_id:
                print(f"已产生半成品文档：document_id={res.document_id}", file=sys.stderr)
                # 半成品也要留档，否则这篇文档没人记得去清理
                _remember_feishu_write(sb, **remember_kw)
            return 2
        print(f"已创建：{res.title}（写入 {res.blocks_written} 块）")
        print(res.url or f"document_id={res.document_id}（配置 feishu.doc_host 可输出链接）")
        _remember_feishu_write(sb, **remember_kw)
        return 0

    if cmd in ("feishu-create-board", "feishu-board-draw", "feishu-boards"):
        return _feishu_board_cmd(sb, cmd, args)

    if cmd == "feishu-edit-body":
        from core.feishu import (
            extract_feishu_urls,
            markdown_to_docx_blocks,
            preview_docx_body,
            update_docx_body,
        )

        refs = extract_feishu_urls(args.url)
        if not refs:
            print("不是有效的飞书文档链接", file=sys.stderr)
            return 2
        ref = refs[0]
        content = _feishu_content_arg(args)
        if content is None:
            return 2
        blocks = markdown_to_docx_blocks(content)
        if not blocks:
            print("正文为空，不做改动", file=sys.stderr)
            return 2
        mode = "replace" if args.replace else "append"
        config_path = args.config or str(default_config_path())
        # 改的是已有内容，确认前先只读看清目标文档，避免改错篇
        if not args.yes:
            pre = preview_docx_body(sb.config.feishu, ref, config_path=config_path)
            if not pre.ok:
                print(f"读取目标文档失败：{pre.error}", file=sys.stderr)
                return 2
            print(f"目标文档：{pre.title or '(标题未知)'}")
            print(f"链接：{pre.url}")
            if mode == "replace":
                print(f"将删除原有 {pre.block_count} 个块，再写入 {len(blocks)} 个块")
                print("（删除可在飞书「历史版本」里恢复，但请先确认改的是这一篇）")
            else:
                print(f"将在末尾追加 {len(blocks)} 个块（现有 {pre.block_count} 块保持不动）")
            if input("确认？(y/N) ").strip().lower() not in {"y", "yes"}:
                print("已取消")
                return 1
        res = update_docx_body(
            sb.config.feishu,
            ref,
            content,
            mode=mode,
            config_path=config_path,
            confirmed=True,
        )
        remember_kw = dict(
            action=res.mode or mode,
            url=res.url,
            title=res.title,
            document_id=res.document_id,
            content=content,
            blocks_written=res.blocks_written,
            blocks_deleted=res.blocks_deleted,
            ok=res.ok,
            error=res.error,
        )
        if not res.ok:
            print(f"改正文失败：{res.error}", file=sys.stderr)
            # 删了却没写成时飞书侧已被改动，必须留档以便去恢复历史版本
            _remember_feishu_write(sb, **remember_kw)
            return 2
        if res.blocks_deleted:
            print(f"已替换正文：删除 {res.blocks_deleted} 块，写入 {res.blocks_written} 块")
        else:
            print(f"已追加正文：写入 {res.blocks_written} 块")
        _remember_feishu_write(sb, **remember_kw)
        return 0

    if cmd == "feishu-comments":
        from core.feishu import extract_feishu_urls, list_docx_comments

        refs = extract_feishu_urls(args.url)
        if not refs:
            print("不是有效的飞书文档链接", file=sys.stderr)
            return 2
        res = list_docx_comments(
            sb.config.feishu,
            refs[0],
            config_path=args.config or str(default_config_path()),
            max_comments=args.max,
        )
        if not res.ok:
            print(f"读评论失败：{res.error}", file=sys.stderr)
            return 2
        if args.json:
            print(
                json.dumps(
                    {
                        "title": res.title,
                        "url": res.url,
                        "truncated": res.truncated,
                        "comments": [vars(c) for c in res.comments],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"{res.title or '(标题未知)'} —— {len(res.comments)} 条评论")
        if res.truncated:
            print(f"（已达 --max {args.max} 上限，可能还有更多）")
        for c in res.comments:
            where = "全文" if c.is_whole else f"局部：{c.quote[:30]}"
            state = "已解决" if c.is_solved else "未解决"
            print(f"\n[{c.comment_id}] {where} | {state}")
            for r in c.replies:
                print(f"  - {r}")
        return 0

    if cmd == "feishu-comment":
        from core.feishu import create_docx_comment, extract_feishu_urls

        refs = extract_feishu_urls(args.url)
        if not refs:
            print("不是有效的飞书文档链接", file=sys.stderr)
            return 2
        ref = refs[0]
        text = (args.text or "").strip()
        if not text:
            print("评论内容为空，不做改动", file=sys.stderr)
            return 2
        # 评论会通知协作者、别人立刻看得见，默认二次确认
        if not args.yes:
            from core.feishu import preview_docx_body

            pre = preview_docx_body(
                sb.config.feishu, ref, config_path=args.config or str(default_config_path())
            )
            if not pre.ok:
                print(f"读取目标文档失败：{pre.error}", file=sys.stderr)
                return 2
            print(f"目标文档：{pre.title or '(标题未知)'}")
            print(f"链接：{pre.url}")
            if args.reply_to:
                print(f"将回复评论 {args.reply_to}：{text}")
            elif args.anchor_text:
                print(f"将在含「{args.anchor_text}」的段落上加局部评论：{text}")
            elif args.block_id:
                print(f"将在块 {args.block_id} 上加局部评论：{text}")
            else:
                print(f"将添加全文评论（显示在文档底部）：{text}")
            print("（评论会通知文档协作者）")
            if input("确认？(y/N) ").strip().lower() not in {"y", "yes"}:
                print("已取消")
                return 1
        res = create_docx_comment(
            sb.config.feishu,
            ref,
            text,
            comment_id=args.reply_to or "",
            block_id=args.block_id or "",
            anchor_text=args.anchor_text or "",
            config_path=args.config or str(default_config_path()),
            confirmed=True,
        )
        if not res.ok:
            print(f"评论失败：{res.error}", file=sys.stderr)
            return 2
        if res.replied_to:
            print(f"已回复评论 {res.replied_to}（新 reply_id={res.reply_id}）")
        elif res.block_id:
            print(
                f"已添加局部评论（comment_id={res.comment_id}，锚定块 {res.block_id}）"
            )
        else:
            print(f"已添加全文评论（comment_id={res.comment_id}）")
        _remember_feishu_write(
            sb,
            action="comment",
            url=res.url,
            title=res.title,
            document_id=res.document_id,
            content=text,
            ok=True,
        )
        return 0

    if cmd == "list":
        print(sb.format_memory_view(args.layer))
        return 0

    if cmd == "status":
        st = sb.status()
        st["persist_dir"] = str(sb.long_term.persist_dir)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if cmd == "backup":
        print(sb.backup_long_term(args.dest))
        return 0

    if cmd == "feishu-subscribe":
        from core.feishu import extract_feishu_urls, subscribe_file_events

        refs = extract_feishu_urls(args.url)
        if not refs:
            print("不是可识别的飞书文档链接", file=sys.stderr)
            return 2
        res = subscribe_file_events(sb.config.feishu, refs[0], config_path=sb.config_path)
        if not res.ok:
            print(f"订阅失败：{res.error}", file=sys.stderr)
            return 1
        who = "应用身份（谁评论都推，包括你自己发的）" if res.identity == "tenant" else (
            "用户身份（只有会给你产生飞书通知的评论才推，自己发的不算）"
        )
        print(f"已订阅 {res.document_id}，{who}")
        return 0

    if cmd == "knowledge-add":
        res = sb.add_knowledge(args.url)
        doc = res.get("doc") or {}
        if not res.get("ok"):
            print(f"入库失败：{res.get('error')}", file=sys.stderr)
            return 1
        if res.get("skipped"):
            print(f"知识库里已有《{doc.get('title')}》，跳过重复抓取")
            return 0
        print(
            f"已收进知识库：《{doc.get('title')}》"
            f"（{doc.get('char_count', 0)} 字 / {doc.get('chunk_count', 0)} 块）"
        )
        return 0

    if cmd == "knowledge-list":
        docs = sb.knowledge.list_docs()
        if not docs:
            print("知识库还是空的。用 knowledge-add <链接> 或 knowledge-backfill 收一些进来。")
            return 0
        for d in docs:
            mark = f"  [失败] {d['last_error']}" if d.get("last_error") else ""
            print(f"- 《{d['title']}》 {d.get('char_count', 0)} 字 / {d.get('chunk_count', 0)} 块{mark}")
            print(f"  {d['url']}")
        print(f"共 {len(docs)} 篇")
        return 0

    if cmd == "knowledge-backfill":
        pending = sb.scan_memory_links(refresh=args.refresh)
        if args.limit > 0:
            pending = pending[: args.limit]
        if not pending:
            print("长时记忆里的飞书文档都已在知识库中，没有要补录的。")
            return 0
        if args.dry_run:
            for item in pending:
                flag = "（已入库，将重抓）" if item["in_kb"] else ""
                print(f"- {item['url']}{flag}\n  来自记忆：{item['question']}")
            print(f"共 {len(pending)} 篇待补录（--dry-run，未抓取）")
            return 0

        def _tick(i, total, item):
            print(f"[{i}/{total}] 抓取 {item['url']}", flush=True)

        res = sb.backfill_knowledge(
            refresh=args.refresh, limit=args.limit, on_progress=_tick
        )
        for d in res["done"]:
            print(f"  ✓ 《{d['title']}》")
        for f in res["failed"]:
            print(f"  ✗ {f['url']}：{f['error']}", file=sys.stderr)
        print(
            f"扫描 {res['scanned']} 条记忆，候选 {res['candidates']} 篇，"
            f"成功 {len(res['done'])} 篇，失败 {len(res['failed'])} 篇"
        )
        return 1 if res["failed"] and not res["done"] else 0

    if cmd == "restore":
        if not args.yes:
            print("将覆盖当前长时记忆。确认请加 --yes", file=sys.stderr)
            return 2
        print(sb.restore_long_term(args.path))
        return 0

    if cmd == "forget":
        if args.keyword is None and not args.yes:
            print(f"将清空记忆层 {args.layer}。确认请加 --yes", file=sys.stderr)
            return 2
        print(sb.forget(keyword=args.keyword, layer=args.layer))
        return 0

    if cmd == "update":
        msg = sb.update_memory(
            memory_id=args.memory_id or "",
            question=args.question or "",
            answer=args.answer,
            new_question=args.new_question or None,
            scene=args.scene or None,
            tags=args.tags,
            kind=args.kind or None,
        )
        print(msg)
        return 0 if not msg.startswith(("未找到", "answer 不能为空")) else 2

    if cmd == "delete":
        print(sb.delete_memory(memory_id=args.memory_id or "", question=args.question or ""))
        return 0

    if cmd == "clear-long":
        if not args.yes:
            n = len(sb.long_term.records)
            print(f"将清空 {n} 条长时记忆。确认请加 --yes（可选 --backup-first）", file=sys.stderr)
            return 2
        print(sb.clear_long_term(backup_first=args.backup_first))
        return 0

    if cmd == "hooks-status":
        st = cursor_hooks.status()
        if as_json:
            print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
            return 0
        print(_format_hooks_status(st))
        return 0

    if cmd == "hooks-install":
        res = cursor_hooks.install(python=args.python or None)
        if as_json:
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
            return 0 if res.ok else 1
        if not res.ok:
            print(f"安装失败：{res.error}", file=sys.stderr)
            return 1
        print(res.message)
        print(f"- 脚本目录：{res.hooks_dir}")
        print(f"- 解释器：{res.python}")
        print(f"- 挂载事件：{', '.join(res.events)}")
        if res.backup:
            print(f"- 原配置已备份：{res.backup}")
        return 0

    if cmd == "hooks-uninstall":
        res = cursor_hooks.uninstall()
        if as_json:
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
            return 0 if res.ok else 1
        if not res.ok:
            print(f"移除失败：{res.error}", file=sys.stderr)
            return 1
        print(res.message)
        if res.backup:
            print(f"- 原配置已备份：{res.backup}")
        return 0

    if cmd == "bot-status":
        st = bot_process.status()
        if as_json:
            print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
            return 0
        print(_format_bot_status(st))
        return 0 if st.running else 1

    if cmd in ("bot-start", "bot-stop", "bot-restart"):
        res = getattr(bot_process, cmd.split("-", 1)[1])()
        if as_json:
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
            return 0 if res.ok else 1
        print(res.message, file=sys.stdout if res.ok else sys.stderr)
        return 0 if res.ok else 1

    if cmd == "scene":
        sb.working.set_scene(args.name)
        print(f"已切换场景为「{sb.working.scene}」")
        return 0

    if cmd == "agent-mode":
        if args.mode is None:
            cur = agent_ui_mode_from_config(sb.config.llm)
            print(
                json.dumps(
                    {
                        "agent_mode": cur,
                        "agent_force": bool(sb.config.llm.agent_force),
                        "runtime": sb.config.llm.runtime,
                        "provider": sb.config.llm.provider,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(sb.set_agent_mode(args.mode, persist=not args.no_persist))
        return 0

    if cmd == "seed":
        seed_dev_memories(sb)
        return 0

    if cmd == "reoptimize":
        n = sb.long_term.reoptimize_all()
        print(f"已对 {n} 条长时记忆重新优化问题（补全别名并刷新向量）。")
        return 0

    if cmd == "interactive":
        if getattr(args, "agent_mode", None):
            print(sb.set_agent_mode(args.agent_mode, persist=True), file=sys.stderr)
        interactive(sb, as_json=as_json, local_only=args.local)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
