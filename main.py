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

from core import MemorySandbox
from core.cli_ui import CliUi
from core.config import agent_ui_mode_from_config, load_config
from core.paths import default_config_path, default_persist_dir
from core.utils import assemble_long_term_query, clean_text


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
    sp = sub.add_parser("backup", help="备份长时记忆")
    sp.add_argument("--dest", default=None, help="备份文件或目录路径")

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

    sp = sub.add_parser("restore", help="从备份恢复长时记忆（覆盖）")
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
