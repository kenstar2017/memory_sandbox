"""文档评论机器人的轮询兜底：定时拉评论，把新回复合成事件走同一条处理链路。

为什么非轮询不可：`drive.notice.comment_add_v1` 是通知型事件，只在「授权用户本人在
飞书客户端收到了这条评论通知」时才推，而飞书从不为你自己的动作通知你自己——所以
**你自己在文档里 @ 机器人，事件永远不会来**。用户维度订阅、按文件订阅（推的是
drive.file.* 那一族，不含评论）、comment_update 订阅三条路都实测过，都盖不过这条规则。

这里只放纯逻辑：挑出哪些回复该处理、把它合成事件 payload。不碰网络、不 import 飞书
SDK，因为这条链路跑错就是「在别人文档里乱说话」，必须能脱机把每个分支都测一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

DOC_COMMENT_EVENT = "drive.notice.comment_add_v1"


@dataclass
class PolledReply:
    """一条待处理的回复，附带解析好的时间戳（用于推进水位）。"""

    created_at: float
    comment: Any
    reply: Any
    # 该回复是评论串里的第一条，也就是评论本身，而不是后续回复
    is_first: bool = False


def parse_epoch(value: Any) -> float:
    """飞书评论的 create_time 是秒级字符串。解析不了返回 0，会被时间下限挡掉。"""
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return 0.0


def collect_new_replies(
    comments: Iterable[Any],
    *,
    since: float,
    seen_keys: Sequence[str] = (),
) -> List[PolledReply]:
    """
    挑出 `since` 之后新增、且没处理过的回复，按时间正序返回。

    刻意**不**在这里判触发词：等确认的改动提案里，用户只回一句「确认」是不带 @ 的，
    在这一层按触发词过滤会把确认吞掉。触发词、白名单、去重全部交给 handle_comment，
    与事件路径共用同一套判断，两条路的行为才不会漂。
    """
    skip = {str(k) for k in seen_keys or ()}
    out: List[PolledReply] = []
    for comment in comments or []:
        replies = list(getattr(comment, "reply_items", None) or [])
        for index, reply in enumerate(replies):
            created = parse_epoch(getattr(reply, "created_at", ""))
            if created < since:
                continue
            if not str(getattr(reply, "text", "") or "").strip():
                continue
            key = reply_key(comment, reply)
            if key in skip:
                continue
            out.append(
                PolledReply(
                    created_at=created,
                    comment=comment,
                    reply=reply,
                    is_first=index == 0,
                )
            )
    out.sort(key=lambda item: item.created_at)
    return out


def reply_key(comment: Any, reply: Any) -> str:
    """与 CommentEvent.key 同构：两条路必须算出同一个键，否则会回两遍。"""
    return f"{getattr(comment, 'comment_id', '')}:{getattr(reply, 'reply_id', '')}"


def synthesize_comment_event(
    *,
    file_token: str,
    file_type: str = "docx",
    item: PolledReply,
) -> Dict[str, Any]:
    """
    按 drive.notice.comment_add_v1 的结构造一个等价 payload。

    复用事件格式而不是另写一条处理函数：去重键是 comment_id:reply_id，两条路造出同一个
    键，于是事件先到还是轮询先到都只会处理一次，而所有判断逻辑只有一份。
    """
    comment = item.comment
    reply = item.reply
    return {
        "header": {"event_type": DOC_COMMENT_EVENT},
        "event": {
            "notice_meta": {
                "file_token": file_token,
                "file_type": file_type,
                "notice_type": "add_comment" if item.is_first else "add_reply",
                "from_user_id": {"open_id": str(getattr(reply, "user_id", "") or "")},
            },
            "comment_id": str(getattr(comment, "comment_id", "") or ""),
            "reply_id": str(getattr(reply, "reply_id", "") or ""),
            # 轮询看不出对方 @ 的是谁，交给 handle_comment 按触发词判断
            "is_mentioned": True,
        },
    }


@dataclass
class PollCursor:
    """
    一篇文档的轮询进度：时间水位 + 水位那一秒已处理过的键。

    只记时间戳不够。create_time 只精确到秒：用 `>= 水位` 会把水位那条每轮都重新捞出来
    （虽然 DocBotState 会拦住不重复回复，但每条都白费一次 batch_query）；改用
    `> 水位` 又会漏掉同一秒里的第二条回复。所以水位边界上的键单独记一份。
    """

    since: float = 0.0
    edge_keys: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.edge_keys is None:
            self.edge_keys = []


def advance_cursor(cursor: PollCursor, items: Sequence[PolledReply]) -> PollCursor:
    """
    按本轮处理过的回复推进游标。

    水位取「已处理的最大时间戳」而不是 time.time()：拉评论和跑模型都要时间，
    用当前时间会把这中间新增的回复永远跳过。
    """
    newest = max((item.created_at for item in items), default=0.0)
    if newest < cursor.since:
        return cursor
    if newest > cursor.since:
        edge = [reply_key(i.comment, i.reply) for i in items if i.created_at == newest]
        return PollCursor(since=newest, edge_keys=edge)
    merged = list(cursor.edge_keys)
    for item in items:
        key = reply_key(item.comment, item.reply)
        if key not in merged:
            merged.append(key)
    return PollCursor(since=cursor.since, edge_keys=merged)


def poll_targets(
    docs: Iterable[Any], extra: Sequence[str] = ()
) -> List[Tuple[str, str]]:
    """
    要盯的文档 → [(document_id, url)]。

    知识库是「关心哪些文档」的天然名单，抓取失败的跳过（多半是死链或没权限，每 30 秒
    重试一次只会白刷日志）。`extra` 收配置里额外指定的，可以是链接也可以是裸 token；
    wiki 链接不收，它的 token 不是 docx token，喂给评论接口取不到东西。
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    for doc in docs or []:
        token = str(getattr(doc, "document_id", "") or "").strip()
        if not token or token in seen:
            continue
        if getattr(doc, "last_error", ""):
            continue
        seen.add(token)
        out.append((token, str(getattr(doc, "url", "") or "")))
    for item in extra or ():
        raw = str(item or "").strip()
        if not raw:
            continue
        url = raw if raw.startswith("http") else ""
        token = raw.rstrip("/").rsplit("/", 1)[-1].split("?")[0] if url else raw
        if not token or token in seen:
            continue
        seen.add(token)
        out.append((token, url))
    return out
