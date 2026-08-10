#!/usr/bin/env python3
"""
记忆沙箱飞书机器人（长连接）

在飞书里私聊机器人 / 群里 @ 它，就能查、写同一份长时记忆——
和 BloomBox、Cursor MCP 用的是一个库。

为什么是长连接：记忆沙箱跑在本机，没有公网地址。Webhook 模式要求开放平台能回调到
一个公网 HTTPS URL，只能靠内网穿透或部署到服务器；长连接只要本机能出网即可。

跑起来：
    pip install lark-oapi
    python3 feishu_bot.py

注意顺序：开放平台「事件与回调 > 事件配置」里把订阅方式改成「使用长连接接收事件」时，
本进程必须已经在跑且已连上，否则那一步保存会失败。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.bot import (
    SeenMessages,
    answer_with_llm,
    message_text,
    remember_with_context,
    respond,
)
from core.config import AppConfig, FeishuConfig, load_config
from core.doc_bot_state import DocBotState, default_state_path
from core.feishu import (
    DOC_REACTION_DONE,
    DOC_REACTION_FAILED,
    DOC_REACTION_WORKING,
    REACTION_DONE,
    REACTION_FAILED,
    REACTION_WORKING,
    FeishuDocRef,
    add_message_reaction,
    create_docx_comment,
    docx_url,
    fetch_bot_identity,
    fetch_bot_message,
    fetch_feishu_document,
    fetch_message_as_user,
    find_docx_block,
    get_file_comment,
    list_chat_messages,
    remove_message_reaction,
    send_bot_text,
    subscribe_user_doc_events,
    update_comment_reaction,
    update_docx_block_text,
    update_docx_body,
)
from core.paths import default_config_path, default_persist_dir

SDK_HINT = (
    "缺少 lark-oapi（飞书官方 SDK，长连接的握手和帧格式是私有的，没法手写）。\n"
    "安装：pip install lark-oapi"
)


def build_sandbox(config_path: str, cfg: Optional[AppConfig] = None) -> MemorySandbox:
    cfg = cfg or load_config(config_path)
    # 与 BloomBox / MCP / CLI 共用同一份用户记忆
    cfg.long_term.persist_dir = str(default_persist_dir())
    clamp_llm_timeout(cfg)
    return MemorySandbox(config=cfg, config_path=config_path)


def clamp_llm_timeout(cfg: AppConfig) -> None:
    """
    聊天场景单独封顶模型超时。

    llm.timeout 默认给的是 600s——那是给「跑一遍完整分析」用的。在飞书里等十分钟
    没有意义：用户早就走了，表情还挂在「处理中」，队列后面的消息也全被堵着。
    只往下压不往上放，配置里本来就更短的照旧。
    """
    cap = float(getattr(cfg.feishu, "bot_llm_timeout", 0) or 0)
    if cap <= 0:
        return
    current = float(getattr(cfg.llm, "timeout", 0) or 0)
    cfg.llm.timeout = cap if current <= 0 else min(current, cap)


class Worker:
    """
    串行跑慢任务的后台线程。

    飞书长连接的事件回调必须 3 秒内返回，否则同一条事件会被重推；而本地没命中要
    交给 agent 现算，几十秒起步。所以回调里只入队，答案由这个线程算完再补发。

    是单线程而不是线程池：慢任务算完要写记忆，同进程并发写 long_term 没有锁
    （文件锁只挡跨进程）。串行也顺带避免了同时开几个 agent 把机器啃满。
    """

    def __init__(self, max_pending: int = 32, name: str = "bot-worker") -> None:
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=max(1, max_pending))
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def submit(self, job: Callable[[], None]) -> bool:
        """入队成功返回 True；队列满返回 False，让调用方降级成即时回复。"""
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            return False
        return True

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception as e:  # noqa: BLE001 - 一个任务炸了不能带走整个线程
                print(f"后台任务失败: {e}", file=sys.stderr)
            finally:
                self._queue.task_done()


def resolve_allow(cfg_allow: List[str]) -> List[str]:
    """环境变量优先，便于临时改白名单而不动配置文件。"""
    env = (os.environ.get("FEISHU_BOT_ALLOW") or "").strip()
    if env:
        return [x.strip() for x in env.replace(";", ",").split(",") if x.strip()]
    return [str(x).strip() for x in (cfg_allow or []) if str(x).strip()]


_reaction_warned: set = set()


def _warn_reaction(err: Exception, scope: str = "消息") -> None:
    """
    表情是可选能力：权限没开就提示一次，别每条消息都刷一行日志。

    按 scope 分开记：消息表情走 im:message.reactions:write_only，评论表情走
    docs:document.comment:create，一边不可用不代表另一边也不行，共用一个开关会
    把另一边唯一的那次提示吞掉。
    """
    if scope in _reaction_warned:
        return
    _reaction_warned.add(scope)
    print(f"{scope}表情回复不可用（后续不再提示）: {err}", file=sys.stderr)


class ReactionStatus:
    """
    用表情回复当进度条：接单贴「处理中」，答完换成「完成」，出错换成「失败」。

    飞书没有「机器人正在输入」这种状态，而这边要重载记忆、可能还要回头拉被引用的
    消息，慢的时候用户只能盯着空气等。表情能立刻告诉他「收到了，在做了」。
    全程失败即忽略——它只是提示，不能让一条回复因此发不出去。
    """

    def __init__(
        self,
        cfg: FeishuConfig,
        *,
        add=add_message_reaction,
        remove=remove_message_reaction,
    ) -> None:
        self._cfg = cfg
        self._add = add
        self._remove = remove
        self._message_id = ""
        self._working_id = ""

    def start(self, message_id: str) -> None:
        try:
            self._working_id = self._add(self._cfg, message_id, REACTION_WORKING)
        except Exception as e:  # noqa: BLE001
            _warn_reaction(e, "消息")
            return
        # 贴上了才记 message_id：贴不上（多半是没开权限）就别再去换「完成」
        self._message_id = message_id

    def finish(self, ok: bool = True) -> None:
        if not self._message_id:
            return
        message_id, working_id = self._message_id, self._working_id
        self._message_id = self._working_id = ""
        try:
            # 先贴终态再撤「处理中」，中间断了也不会落得一个表情都没有
            self._add(self._cfg, message_id, REACTION_DONE if ok else REACTION_FAILED)
            self._remove(self._cfg, message_id, working_id)
        except Exception as e:  # noqa: BLE001
            _warn_reaction(e, "消息")


class CommentReactionStatus:
    """
    评论区的进度条：接单贴「处理中」，做完换成「完成」或「失败」。

    评论里查记忆 + 跑模型动辄几十秒，在此之前这条串里一片安静，提问的人只能干等。
    原先靠 `_AckTimer` 在 8 秒后补一条「收到」的文字回复，可评论串里每条回复
    整篇文档的协作者都看得见，比一个表情吵得多——所以表情贴上了就不发那条文字回执，
    只在贴不上（多半是权限没开）时退回去。

    和 IM 那套（`ReactionStatus`）的差别：云文档的表情挂在 **reply_id** 上而不是
    comment_id，撤销只要 reply_id + reaction_type，没有也不需要 reaction_id。
    """

    def __init__(
        self,
        cfg: FeishuConfig,
        ref: FeishuDocRef,
        reply_id: str,
        config_path: str = "",
        *,
        react=update_comment_reaction,
    ) -> None:
        self._cfg = cfg
        self._ref = ref
        self._reply_id = reply_id or ""
        self._config_path = config_path
        self._react = react
        self._active = False

    @property
    def active(self) -> bool:
        """「处理中」确实贴上去了；没贴上就得靠文字回执兜底。"""
        return self._active

    def _set(self, reaction_type: str, action: str) -> bool:
        return bool(
            self._react(
                self._cfg,
                self._ref,
                self._reply_id,
                reaction_type,
                action=action,
                config_path=self._config_path or None,
                # 打开 doc_bot_enabled 就等于本人预先同意了机器人在被点名的这串里表态
                confirmed=True,
            )
        )

    def start(self) -> None:
        if not self._reply_id:
            return
        try:
            self._active = self._set(DOC_REACTION_WORKING, "add")
        except Exception as e:  # noqa: BLE001 - 表情只是提示，不能挡住正事
            _warn_reaction(e, "评论")

    def finish(self, ok: bool = True) -> None:
        if not self._active:
            return
        self._active = False
        try:
            # 先贴终态再撤「处理中」，中间断了也不会落得一个表情都没有
            self._set(DOC_REACTION_DONE if ok else DOC_REACTION_FAILED, "add")
            self._set(DOC_REACTION_WORKING, "delete")
        except Exception as e:  # noqa: BLE001
            _warn_reaction(e, "评论")


def event_summary(payload: dict) -> str:
    """
    一条消息事件的可辨识摘要。**不带正文**——日志是排障用的，不是聊天记录副本。

    没有这行日志，「消息到底有没有到本机」就无从判断：SDK 只在事件类型没有处理器
    时才打日志，而 im.message.receive_v1 是注册过的，收到与没收到在日志里长得一模一样。
    这个坑踩过两次了，每次都要重启成 --debug 再让对方重发一遍。
    """
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    if not isinstance(event, dict):
        return "无法解析"
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    # 带上时间：SDK 自己的日志有时间戳，这几行没有的话就没法跟对方发来的截图对上
    parts = [
        time.strftime("%H:%M:%S"),
        f"chat_type={message.get('chat_type') or '?'}",
        f"type={message.get('message_type') or '?'}",
        f"mid={message.get('message_id') or '?'}",
        f"from={(sender.get('sender_id') or {}).get('open_id') or '?'}",
    ]
    if message.get("parent_id") or message.get("root_id"):
        parts.append("引用=有")
    return " ".join(parts)


def fetch_quoted(
    cfg: FeishuConfig, message_id: str, *, config_path: str = ""
) -> Optional[Tuple[str, str]]:
    """
    读「用户回复的那一条」；应用身份**读不动**（报错）时换用户身份再试一次。

    别的应用发的卡片只会返回一个 157 字节的摘要外壳（一个 image_key 加两个空文本）。
    曾经以为换用户身份能拿到全文，2026-08-06 实测证伪：授权了 im:message:readonly 之后，
    用户身份返回的字节数与应用身份**一模一样**。飞书就是不把跨应用的卡片正文下发给任何
    身份，与权限无关，所以「空壳就重试一次」纯属白打一次接口，已删掉——别照着旧结论加回来。

    保留的只有「应用身份直接失败」那条路：缺 im:message.group_msg 之类时，
    用户身份确实还能读到。补读再失败也不往上抛应用身份以外的错，
    读消息这条路不能因为补读挂掉。
    """
    try:
        return fetch_bot_message(cfg, message_id)
    except Exception as first:  # noqa: BLE001 - 应用身份没权限时，用户身份可能还行
        try:
            return fetch_message_as_user(cfg, message_id, config_path=config_path or None)
        except Exception as second:  # noqa: BLE001
            print(f"  用户身份补读也失败: {second}", file=sys.stderr)
            raise first


def dispatch(
    payload: dict,
    *,
    sandbox: MemorySandbox,
    config: AppConfig,
    allow: List[str],
    seen: SeenMessages,
    send=send_bot_text,
    fetch=fetch_bot_message,
    history=list_chat_messages,
    react_add=add_message_reaction,
    react_remove=remove_message_reaction,
    worker: Optional[Worker] = None,
    self_open_id: str = "",
    self_name: str = "",
) -> Optional[str]:
    """
    一条事件的完整处理：想好回什么，再发出去。返回回复的 message_id。

    本地检索是毫秒级的，就在回调里做完；只有「本地没命中、要交给 agent 现算」这种
    几十秒的活才丢给 worker，回调立刻返回（此时返回 None，回复由 worker 补发）。
    没给 worker 就原地跑完——单测和 --once 这类同步场景要的是确定性。

    单独抽出来是因为这层是「纯逻辑」与「SDK 回调」之间的接缝——
    真出过一次 bug：把整个 AppConfig 传给了只认 FeishuConfig 的 send_bot_text，
    两边各自的单测都是绿的，只有接缝没人看。
    """
    print(f"收到消息 {event_summary(payload)}")
    status = ReactionStatus(config.feishu, add=react_add, remove=react_remove)
    out = respond(
        sandbox,
        payload,
        allow=allow,
        seen=seen,
        fetch_message=lambda mid: fetch(config.feishu, mid),
        fetch_context=lambda chat_id, before: history(
            config.feishu, chat_id, before_message_id=before
        ),
        on_start=status.start,
        self_open_id=self_open_id,
        self_name=self_name,
    )
    if out is None:
        print(
            "  → 不处理（自己发的 / 重复投递 / 取不到文本 / 群里没 @ 我 / "
            "别的机器人但不在白名单）"
        )
        return None

    def deliver(text: str) -> Optional[str]:
        try:
            sent = send(config.feishu, text, reply_to=out.reply_to)
        except Exception as e:  # noqa: BLE001 - 发不出去不能让长连接跟着挂
            print(f"回消息失败: {e}", file=sys.stderr)
            status.finish(ok=False)
            return None
        status.finish(ok=True)
        print(f"  → 已回复 {sent or '(未取到 message_id)'}")
        return sent

    if out.slow_query:
        # 「处理中」的表情一直挂着，用户知道还在算
        def slow() -> None:
            if out.slow_context:
                deliver(remember_with_context(sandbox, out.slow_query, out.slow_context))
            else:
                deliver(answer_with_llm(sandbox, out.slow_query))

        if worker is None:
            slow()
            return None
        if worker.submit(slow):
            if out.slow_context:
                print(f"  → 卡片读不到，改用群里上游 {len(out.slow_context)} 字上下文，交给模型")
            else:
                print("  → 本地没命中，交给模型，答完再补发")
            return None
        print("后台队列已满，退回本地检索结果", file=sys.stderr)

    return deliver(out.text)


def _post_comment(
    reply,
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    comment_id: str,
    text: str,
    config_path: str = "",
) -> bool:
    """
    往被 @ 的那条评论串里回一条。

    这里传 confirmed=True 是有意的：打开 doc_bot_enabled 就等于本人预先同意了
    「白名单成员在评论里点名我时，可以自动回复这一串」。**改正文不在此列**——
    正文改动仍然一条一条单独提案、等本人回「确认」才落笔。

    as_app=True：机器人的回复要署机器人的名。评论接口两种 token 都收，署名跟着
    token 走，早先整条链路都用 user token，于是机器人的话在文档里署的是本人的名字，
    只能靠正文前缀「BloomBot 自动回复」自证。
    """
    res = reply(
        cfg,
        ref,
        text,
        comment_id=comment_id,
        config_path=config_path or None,
        confirmed=True,
        as_app=True,
    )
    if not getattr(res, "ok", False):
        print(f"回评论失败: {getattr(res, 'error', '')}", file=sys.stderr)
        return False
    return True


def _answer_comment(sandbox, question: str, context: str) -> str:
    """记忆库参考 + 文档正文 → 结论。没配模型就退回本地检索到的东西。"""
    from core.doc_bot import ANSWER_CONTRACT, clean_model_text

    llm = getattr(sandbox, "llm", None)
    if llm is None:
        return ""
    prompt = f"{ANSWER_CONTRACT}\n\n【问题】{question}"
    return clean_model_text(llm.generate(prompt, context=context))


def handle_comment(
    payload: dict,
    *,
    sandbox: MemorySandbox,
    config: AppConfig,
    allow: List[str],
    state: "DocBotState",
    config_path: str = "",
    get_comment=get_file_comment,
    reply=create_docx_comment,
    read_doc=fetch_feishu_document,
    find_block=find_docx_block,
    edit_block=update_docx_block_text,
    append_body=update_docx_body,
    react=update_comment_reaction,
    ack_delay: Optional[float] = None,
    self_open_id: str = "",
) -> str:
    """
    一条文档评论事件的完整处理。返回一个状态串，只用于日志和单测。

    所有飞书调用都能注入，是因为这条链路一旦跑错就是「在别人文档里乱说话/乱改」，
    必须能脱机把每个分支都测一遍。
    """
    from core.doc_bot import (
        authorized,
        build_answer_context,
        classify,
        comment_question,
        format_ack,
        format_answer,
        format_cancelled,
        format_cannot,
        format_needs_confirmation,
        format_proposal,
        is_bot_reply,
        is_cancellation,
        is_confirmation,
        looks_like_confirmation_attempt,
        mentions_bot,
        parse_comment_event,
        pick_reply_id,
        pick_reply_text,
        strip_trigger,
    )
    from core.doc_bot_state import EditProposal

    ev = parse_comment_event(payload)
    if ev is None:
        return "skip:not-a-comment"
    cfg = config.feishu
    if not getattr(cfg, "doc_bot_enabled", False):
        return "skip:disabled"
    if ev.file_type != "docx":
        # 表格、多维表格的块结构完全不同，V1 不碰
        return f"skip:file_type={ev.file_type}"
    if not authorized(ev.open_id, allow):
        return "skip:not-allowed"
    if not state.check_and_mark(ev.key):
        return "skip:duplicate"

    ref = FeishuDocRef(url=docx_url(cfg, ev.file_token), kind="docx", token=ev.file_token)
    got = get_comment(cfg, ref, ev.comment_id, config_path=config_path or None)
    if not getattr(got, "ok", False) or not got.comments:
        print(f"读评论失败: {getattr(got, 'error', '')}", file=sys.stderr)
        return "error:read-comment"
    comment = got.comments[0]
    replies = getattr(comment, "reply_items", [])
    text = pick_reply_text(replies, ev.reply_id)
    if not text:
        return "skip:empty"
    if is_bot_reply(text):
        # 自己的回复里带着「BloomBot」，不挡就会自己触发自己
        return "skip:self"

    # 机器人真在这篇文档里说过话，就把整篇收进知识库。挂在 post() 这个唯一的回复出口
    # 上，而不是在每个分支各调一次：分支以后还会加，漏掉一个就是静默不入库。它保持
    # 沉默（不在白名单、没被 @、串里别人的讨论）时一次都不触发
    ingested = False
    force_kb = False  # 自己改过正文的那条路要重抓，否则库里留的是被它推翻的旧正文

    def post(body: str) -> bool:
        nonlocal ingested
        ok = _post_comment(reply, cfg, ref, ev.comment_id, body, config_path)
        if ok and not ingested:
            ingested = True
            _ingest_doc_quietly(sandbox, ev.file_token, url=ref.url, force=force_kb)
        return ok

    # 只在确认要干活的分支上 start()：贴到无关回复上等于替别人的讨论表态
    reaction = CommentReactionStatus(
        cfg, ref, pick_reply_id(replies, ev.reply_id), config_path, react=react
    )
    trigger = getattr(cfg, "doc_bot_trigger", "") or "@BloomBot"
    pending = state.peek_pending(ev.comment_id)
    if pending is not None:
        if is_confirmation(text):
            plan = state.take_pending(ev.comment_id)  # 先摘掉，免得「确认」两遍改两次
            if plan is None:
                return "skip:race"
            reaction.start()
            force_kb = True  # 马上要动正文，知识库那份必须重抓
            got_status = _apply_plan(
                plan,
                cfg=cfg,
                ref=ref,
                config_path=config_path,
                post=post,
                edit_block=edit_block,
                append_body=append_body,
            )
            reaction.finish(ok=got_status == "applied")
            return got_status
        if is_cancellation(text):
            state.take_pending(ev.comment_id)
            post(format_cancelled())
            return "cancelled"
        if not mentions_bot(text, trigger, self_open_id):
            # 串里的其它讨论不插嘴；但像是想确认又没说清的，提醒一句
            if looks_like_confirmation_attempt(text):
                post(format_needs_confirmation())
                return "nudged"
            return "skip:no-trigger"
    elif not mentions_bot(text, trigger, self_open_id):
        return "skip:no-trigger"

    instruction = strip_trigger(text, trigger)
    if not instruction:
        post(format_cannot("只看到 @ 我，没看到要我做什么"))
        return "empty-instruction"

    reaction.start()
    # 表情贴上了就不必再补「收到」那条文字回复——评论串里每条回复整篇文档的协作者
    # 都看得见，表情安静得多。只有贴不上（多半是权限没开）才退回文字回执
    delay = (
        0.0
        if reaction.active
        else (
            ack_delay
            if ack_delay is not None
            else float(getattr(cfg, "doc_bot_ack_after_seconds", 8) or 0)
        )
    )
    ack = _AckTimer(delay, lambda: post(format_ack()))
    ack.start()
    status = "error:unknown"
    try:
        if classify(instruction) == "edit":
            plan, err = _plan_edit(
                instruction,
                comment=comment,
                cfg=cfg,
                ref=ref,
                config_path=config_path,
                find_block=find_block,
                sandbox=sandbox,
            )
            if plan is None:
                ack.done()
                post(format_cannot(err))
                status = "cannot-edit"
                return status
            state.put_pending(
                EditProposal(
                    file_token=ev.file_token,
                    file_type=ev.file_type,
                    comment_id=ev.comment_id,
                    block_id=plan.block_id,
                    old_text=plan.old_text,
                    new_text=plan.new_text,
                    append=plan.append,
                    why=plan.why,
                )
            )
            ack.done()
            post(format_proposal(plan))
            status = "proposed"
            return status

        try:
            refs = (sandbox.build_reference_pack(instruction, top_k=3) or {}).get(
                "references"
            ) or []
        except Exception as e:  # noqa: BLE001 - 检索挂了也该让模型接着答
            print(f"记忆检索失败: {e}", file=sys.stderr)
            refs = []
        # 正文只有回答时才用得上：改动那条路的原文来自定位到的那个块，不必整篇拉
        doc = read_doc(cfg, ref, config_path=config_path or None)
        doc_text = getattr(doc, "content", "") if getattr(doc, "ok", False) else ""
        doc_title = getattr(doc, "title", "") or getattr(got, "title", "")
        context = build_answer_context(
            doc_title=doc_title,
            quote=getattr(comment, "quote", ""),
            doc_text=doc_text,
            references=refs,
        )
        try:
            answer = _answer_comment(sandbox, instruction, context)
        except Exception as e:  # noqa: BLE001
            ack.done()
            post(format_cannot(f"问模型失败：{e}"))
            status = "error:llm"
            return status
        if not answer:
            answer = _local_only_answer(refs)
        if not answer:
            ack.done()
            post(format_cannot("记忆库里没有相关内容，也没配可用的模型"))
            status = "no-answer"
            return status
        ack.done()
        post(format_answer(answer))
        _remember_quietly(
            sandbox,
            comment_question(instruction, getattr(comment, "quote", "")),
            answer,
            doc_title,
        )
        status = "answered"
        return status
    finally:
        ack.cancel()
        # 没答上来也要换表情：留着「处理中」不动，用户会一直以为还在跑
        reaction.finish(ok=status in ("answered", "proposed"))


def _local_only_answer(references: List[dict]) -> str:
    """没配模型时的退路：把最相关的一条记忆原样给出去，并说明它只是参考。"""
    if not references:
        return ""
    top = references[0] or {}
    question = str(top.get("question") or "").strip()
    answer = str(top.get("answer") or "").strip()
    if not answer:
        return ""
    return f"记忆库里最相关的一条（未接模型，仅供参考）——{question}\n{answer}"


def _ingest_doc_quietly(sandbox, file_token: str, *, url: str = "", force: bool = False) -> None:
    """把机器人回复过的那篇文档整篇收进知识库。

    只入队，抓取在后台线程做：这里是评论回调线程，拉全文要好几秒。
    附带效果是那篇文档进了知识库之后，下次启动会被按文件订阅、也进评论轮询范围
    （见 `_subscribe_knowledge_docs` 与 `_start_comment_poller`），以后不必手工订阅。
    """
    try:
        sandbox.queue_knowledge_doc(file_token, url=url, origin="doc-comment", force=force)
    except Exception as e:  # noqa: BLE001 - 入库失败不能影响已经发出去的回复
        print(f"知识库入库失败: {e}", file=sys.stderr)


def _remember_quietly(sandbox, question: str, answer: str, doc_title: str) -> None:
    try:
        tail = f"（来自《{doc_title}》评论）" if doc_title else "（来自飞书文档评论）"
        sandbox.remember(question, f"{answer}\n{tail}", scene="dev", tags=["doc-comment"])
    except Exception as e:  # noqa: BLE001 - 记不住也不能影响已经发出去的回复
        print(f"写记忆失败: {e}", file=sys.stderr)


def _plan_edit(
    instruction: str,
    *,
    comment,
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    config_path: str,
    find_block,
    sandbox,
):
    """把「要改成什么」算出来，但一个字都不写。返回 (EditPlan, 错误说明)。"""
    from core.doc_bot import (
        EditPlan,
        build_edit_prompt,
        clean_model_text,
        extract_append_text,
        extract_replacement,
        wants_append,
    )

    quote = (getattr(comment, "quote", "") or "").strip()
    if not quote:
        if not wants_append(instruction):
            return None, (
                "这是全文评论，我定位不到要改哪一段。"
                "请在正文里选中那句话发划词评论再 @ 我；"
                "或者说清楚是「在文末补充 …」。"
            )
        body = extract_append_text(instruction)
        if not body:
            return None, "看不出要追加什么内容"
        return EditPlan(new_text=body, append=True, why="按要求在文末追加"), ""

    try:
        block_id, block_text = find_block(cfg, ref, quote, config_path=config_path or None)
    except Exception as e:  # noqa: BLE001 - 定位不到就说清楚，绝不猜一个块去改
        return None, f"定位不到要改的段落：{e}"

    new_text, why = extract_replacement(instruction, block_text)
    if not new_text:
        llm = getattr(sandbox, "llm", None)
        if llm is None:
            return None, "没看懂要改成什么，也没配模型帮忙改写。可以直接说「改成 …」"
        try:
            new_text = clean_model_text(
                llm.generate(build_edit_prompt(instruction, block_text, quote=quote))
            )
        except Exception as e:  # noqa: BLE001
            return None, f"改写失败：{e}"
        why = "按评论要求改写"
    if not new_text:
        return None, "没算出改动内容"
    if new_text.strip() == (block_text or "").strip():
        return None, "改完和原文一样，就不动了"
    return EditPlan(block_id=block_id, old_text=block_text, new_text=new_text, why=why), ""


def _apply_plan(
    proposal,
    *,
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    config_path: str,
    post,
    edit_block,
    append_body,
) -> str:
    """本人回了「确认」才会走到这里。这是全流程里唯一真的写正文的地方。"""
    from core.doc_bot import EditPlan, format_applied, format_cannot

    plan = EditPlan(
        block_id=proposal.block_id,
        old_text=proposal.old_text,
        new_text=proposal.new_text,
        append=proposal.append,
        why=proposal.why,
    )
    if plan.append:
        res = append_body(
            cfg,
            ref,
            plan.new_text,
            mode="append",
            config_path=config_path or None,
            confirmed=True,
        )
    else:
        res = edit_block(
            cfg,
            ref,
            plan.block_id,
            plan.new_text,
            # 提案和确认之间可能隔了很久，中间别人改过就不能覆盖
            expect_text=plan.old_text,
            config_path=config_path or None,
            confirmed=True,
        )
    if not getattr(res, "ok", False):
        post(format_cannot(f"{getattr(res, 'error', '未知错误')}；提案已作废，需要的话重新 @ 我"))
        return "error:apply"
    post(format_applied(plan))
    return "applied"


class _AckTimer:
    """
    慢了才回「收到」。

    评论区不是聊天窗口，每条回复所有协作者都看得见，所以不能一上来就先刷一条
    「正在处理」。但查记忆 + 跑 agent 可能要几十秒，久到用户以为没人理，
    所以超过阈值再补一条。
    """

    def __init__(self, delay: float, fire) -> None:
        self._delay = float(delay or 0)
        self._fire = fire
        self._lock = threading.Lock()
        self._finished = False
        self._timer: Optional[threading.Timer] = None

    def start(self) -> None:
        if self._delay <= 0:
            return
        self._timer = threading.Timer(self._delay, self._run)
        self._timer.daemon = True
        self._timer.start()

    def _run(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        try:
            self._fire()
        except Exception as e:  # noqa: BLE001 - 回执发不出去不影响正事
            print(f"发收到回执失败: {e}", file=sys.stderr)

    def done(self) -> None:
        """结论已经算完、马上要发了：别再补「收到」。"""
        with self._lock:
            self._finished = True
        self.cancel()

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()


def check(config_path: str) -> int:
    """不连飞书，只体检配置，方便定位「为什么机器人不理我」。"""
    cfg = load_config(config_path)
    allow = resolve_allow(getattr(cfg.feishu, "bot_allow_open_ids", []))
    print(f"配置文件      : {config_path}")
    print(f"记忆目录      : {default_persist_dir()}")
    print(f"app_id        : {'已配置' if cfg.feishu.app_id else '缺失'}")
    print(f"app_secret    : {'已配置' if cfg.feishu.app_secret else '缺失'}")
    print(f"白名单 open_id: {allow or '（空：任何人私聊都只会收到自己的 open_id）'}")
    doc_bot = bool(getattr(cfg.feishu, "doc_bot_enabled", False))
    print(
        "文档评论机器人: "
        + (
            f"开（触发词 {getattr(cfg.feishu, 'doc_bot_trigger', '')}）"
            if doc_bot
            else "关（feishu.doc_bot_enabled=false）"
        )
    )
    try:
        import lark_oapi  # noqa: F401

        print("lark-oapi     : 已安装")
    except ImportError:
        print("lark-oapi     : 未安装 —— " + SDK_HINT.replace("\n", " "))
        return 2
    return 0 if (cfg.feishu.app_id and cfg.feishu.app_secret) else 2


def _comment_job(
    payload: dict,
    *,
    sandbox: MemorySandbox,
    config: AppConfig,
    allow: List[str],
    state: DocBotState,
    config_path: str,
    source: str = "事件",
    self_open_id: str = "",
) -> Callable[[], None]:
    """把一条评论的处理包成后台任务。事件与轮询共用，保证两条路行为一致。"""

    def job() -> None:
        try:
            result = handle_comment(
                payload,
                sandbox=sandbox,
                config=config,
                allow=allow,
                state=state,
                config_path=config_path,
                self_open_id=self_open_id,
            )
        except Exception as e:  # noqa: BLE001 - 评论链路再怎么错也不能带走长连接
            print(f"处理评论失败: {e}", file=sys.stderr)
            return
        if not result.startswith("skip:"):
            print(f"评论{source} {result}")
        elif source == "事件":
            # skip 也要留痕。曾经因为 skip 全静默，把「事件到了但被触发词判断丢掉」
            # 一路误诊成「飞书根本没推事件」，绕着权限和订阅折腾了半天。
            # 轮询是自己把整篇评论捞回来的，大量 skip 属正常，不打。
            print(f"评论事件 {result}", file=sys.stderr)

    return job


def _subscribe_knowledge_docs(cfg, config_path: Optional[str]) -> None:
    """
    把知识库里的文档逐篇按文件订阅，让它们的评论能推到机器人。

    `subscribe_user_doc_events` 那条用户维度订阅只在「你本人收到了评论通知」时才推，
    所以没订阅过的文档里别人评论也未必推得到；按文件订阅补上这一层。
    放后台线程：每篇一次网络往返，十几篇会把启动拖成十几秒，而 IM 那侧不该等它。
    """
    import threading

    def run() -> None:
        from core import MemorySandbox
        from core.feishu import FeishuDocRef, subscribe_file_events

        try:
            docs = MemorySandbox(config_path=config_path).knowledge.docs
        except Exception as e:  # noqa: BLE001 - 附加能力，绝不能拖垮机器人启动
            print(f"警告：读知识库失败，跳过按文件订阅：{e}", file=sys.stderr)
            return
        ok = 0
        failed = []
        for doc in docs:
            token = (doc.document_id or "").strip()
            if not token or doc.last_error:
                continue
            res = subscribe_file_events(
                cfg.feishu,
                FeishuDocRef(url=doc.url, kind="docx", token=token),
                config_path=config_path,
            )
            if res.ok:
                ok += 1
            else:
                failed.append(res.error)
        if ok or failed:
            print(f"知识库文档按文件订阅：成功 {ok} 篇，失败 {len(failed)} 篇")
        if failed:
            print(f"     首个失败原因：{failed[0]}", file=sys.stderr)

    threading.Thread(target=run, name="kb-subscribe", daemon=True).start()


def _start_comment_poller(
    cfg: AppConfig,
    config_path: str,
    *,
    sandbox: MemorySandbox,
    allow: List[str],
    state: DocBotState,
    worker: "Worker",
    self_open_id: str = "",
) -> None:
    """
    定时拉知识库文档的评论，把新回复合成事件走 handle_comment。

    这不是给事件加保险，而是唯一能覆盖「自己发的评论」的路：飞书的评论事件只在
    授权用户本人收到通知时才推，而它从不为你自己的动作通知你自己。别人评论仍走事件，
    两条路用同一个去重键（comment_id:reply_id），谁先到都只处理一次。
    """
    interval = float(getattr(cfg.feishu, "doc_bot_poll_seconds", 30) or 0)
    if interval <= 0:
        print("文档评论轮询已关闭（feishu.doc_bot_poll_seconds<=0），自己发的评论不会触发")
        return

    def run() -> None:
        from core.doc_bot_poll import (
            PollCursor,
            advance_cursor,
            collect_new_replies,
            poll_targets,
            synthesize_comment_event,
        )
        from core.feishu import list_docx_comments

        max_docs = max(1, int(getattr(cfg.feishu, "doc_bot_poll_max_docs", 40) or 40))
        # 只看进程启动之后的评论：知识库里堆着几百条历史评论，第一轮全当新的会疯狂刷屏
        floor = time.time()
        cursors: dict = {}
        warned: set = set()
        while True:
            time.sleep(interval)
            try:
                sandbox.knowledge.reload()
                targets = poll_targets(
                    sandbox.knowledge.docs,
                    getattr(cfg.feishu, "doc_bot_poll_extra", None) or [],
                )[:max_docs]
            except Exception as e:  # noqa: BLE001 - 兜底能力，绝不能带走机器人
                print(f"评论轮询：读知识库失败 {e}", file=sys.stderr)
                continue
            for token, url in targets:
                cursor = cursors.get(token) or PollCursor(since=floor)
                ref = FeishuDocRef(
                    url=url or docx_url(cfg.feishu, token), kind="docx", token=token
                )
                try:
                    got = list_docx_comments(
                        cfg.feishu,
                        ref,
                        config_path=config_path,
                        resolve_title=False,
                    )
                except Exception as e:  # noqa: BLE001
                    got = None
                    err = str(e)
                else:
                    err = "" if got.ok else got.error
                if err:
                    # 同一篇的同一个错误只报一次，否则没权限的文档会每 30 秒刷一行
                    mark = f"{token}:{err[:60]}"
                    if mark not in warned:
                        warned.add(mark)
                        print(f"评论轮询：{token} 拉评论失败 {err}", file=sys.stderr)
                    continue
                items = collect_new_replies(
                    got.comments, since=cursor.since, seen_keys=cursor.edge_keys
                )
                cursors[token] = advance_cursor(cursor, items)
                for item in items:
                    payload = synthesize_comment_event(file_token=token, item=item)
                    if not worker.submit(
                        _comment_job(
                            payload,
                            sandbox=sandbox,
                            config=cfg,
                            allow=allow,
                            state=state,
                            config_path=config_path,
                            source="轮询",
                            self_open_id=self_open_id,
                        )
                    ):
                        print("后台队列已满，丢弃一条轮询到的评论", file=sys.stderr)

    threading.Thread(target=run, name="doc-comment-poll", daemon=True).start()
    print(f"文档评论轮询已开启：每 {interval:.0f} 秒扫一遍知识库文档")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="记忆沙箱飞书机器人（长连接）")
    parser.add_argument("--config", default="", help="配置文件路径")
    parser.add_argument("--check", action="store_true", help="只体检配置，不建立连接")
    parser.add_argument("--debug", action="store_true", help="打印 SDK 调试日志")
    args = parser.parse_args(argv)

    config_path = args.config or str(default_config_path())
    if args.check:
        return check(config_path)

    try:
        import lark_oapi as lark
    except ImportError:
        print(SDK_HINT, file=sys.stderr)
        return 2

    cfg = load_config(config_path)
    app_id = (cfg.feishu.app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (cfg.feishu.app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        print("未配置 feishu.app_id / app_secret，先填好配置再启动", file=sys.stderr)
        return 2

    allow = resolve_allow(getattr(cfg.feishu, "bot_allow_open_ids", []))
    if not allow:
        print(
            "警告：白名单为空，机器人不会回答任何内容，只会告诉对方他的 open_id。\n"
            "     私聊一句拿到自己的 open_id，填进 config.yaml 的 "
            "feishu.bot_allow_open_ids 再重启。",
            file=sys.stderr,
        )

    # 群里只回 @ 到自己的消息，得先知道自己是谁：事件里的 mentions 给的是 open_id 和显示名，
    # 配置里只有 app_id，对不上。取不到就退回「群里照旧全回」，并把原因说清楚
    self_open_id, self_name = "", ""
    try:
        self_open_id, self_name = fetch_bot_identity(cfg.feishu)
        print(f"机器人身份：{self_name or '(无名字)'} {self_open_id or '(无 open_id)'}")
    except Exception as e:  # noqa: BLE001 - 认不出自己只影响群里的 @ 判断
        print(
            f"警告：读不到机器人自己的 open_id（{e}）；"
            "群里将无法判断有没有 @ 到自己，会照旧回复每一条消息",
            file=sys.stderr,
        )

    sandbox = build_sandbox(config_path, cfg)
    seen = SeenMessages()
    worker = Worker()
    worker.start()

    def on_message(data) -> None:
        try:
            payload = json.loads(lark.JSON.marshal(data))
        except Exception as e:  # noqa: BLE001
            print(f"事件解析失败: {e}", file=sys.stderr)
            return
        dispatch(
            payload,
            sandbox=sandbox,
            config=cfg,
            allow=allow,
            seen=seen,
            worker=worker,
            fetch=lambda fcfg, mid: fetch_quoted(fcfg, mid, config_path=config_path),
            self_open_id=self_open_id,
            self_name=self_name,
        )

    doc_bot_on = bool(getattr(cfg.feishu, "doc_bot_enabled", False))
    state = DocBotState(default_state_path())

    def on_comment(data) -> None:
        try:
            payload = json.loads(lark.JSON.marshal(data))
        except Exception as e:  # noqa: BLE001
            print(f"评论事件解析失败: {e}", file=sys.stderr)
            return

        job = _comment_job(
            payload,
            sandbox=sandbox,
            config=cfg,
            allow=allow,
            state=state,
            config_path=config_path,
            source="事件",
            self_open_id=self_open_id,
        )
        # 回调必须 3 秒内返回，而这条链路要拉正文、跑模型，只能异步
        if not worker.submit(job):
            print("后台队列已满，丢弃一条评论事件", file=sys.stderr)

    builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        on_message
    )
    if doc_bot_on:
        register = getattr(builder, "register_p2_drive_notice_comment_add_v1", None)
        if register is None:
            print(
                "警告：当前 lark-oapi 不支持云文档评论事件，文档评论机器人不会生效；"
                "升级：pip install -U lark-oapi",
                file=sys.stderr,
            )
        else:
            builder = register(on_comment) or builder
            try:
                subscribe_user_doc_events(cfg.feishu, config_path=config_path)
                print("已订阅云文档评论事件（drive.notice.comment_add_v1）")
            except Exception as e:  # noqa: BLE001 - 订阅失败只是收不到评论，IM 照常
                print(
                    f"警告：订阅云文档评论事件失败，评论机器人收不到事件：{e}\n"
                    "     多半是没授权 docs:event:subscribe；"
                    "重跑 python3 scripts/feishu_login.py 再试",
                    file=sys.stderr,
                )
            _subscribe_knowledge_docs(cfg, config_path)
            _start_comment_poller(
                cfg,
                config_path,
                sandbox=sandbox,
                allow=allow,
                state=state,
                worker=worker,
                self_open_id=self_open_id,
            )
    handler = builder.build()
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG if args.debug else lark.LogLevel.INFO,
    )
    print("记忆沙箱飞书机器人已启动，等待消息…（Ctrl+C 退出）")
    try:
        client.start()
    except KeyboardInterrupt:
        print("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
