"""文档评论机器人的纯逻辑层：评论事件 → 该回什么 / 该改什么。

和 core/bot.py 同一套分层思路：这里不 import lark_oapi、不发任何网络请求，
所以门禁、分类、确认识别这些真正容易出错的地方都能脱离飞书环境跑单测。

安全边界（写死在这里，别绕过）：
- 只在被 @ 的那条评论串里说话，不新开评论、不碰没被 @ 过的文档
- 改正文只做「换掉一个块」和「文末追加」，不做全文重写
- 一律先提案、等本人回「确认」再落笔，确认词只认明确表态，模棱两可就再问一次
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 回复统一带前缀：评论走的是本人的 user token，署名是你，不写清楚同事会以为是你手打的
BOT_PREFIX = "🤖 BloomBot 自动回复"
# 评论框放不下长文，也不该把整段记忆糊上去
MAX_COMMENT_CHARS = 1200
# 注入模型的正文摘录上限
MAX_DOC_CHARS = 6000

_QUOTES = "「」『』“”\"'‘’《》"


@dataclass
class CommentEvent:
    """drive.notice.comment_add_v1 的关键字段。事件不带正文，正文要另取。"""

    file_token: str = ""
    file_type: str = "docx"
    comment_id: str = ""
    reply_id: str = ""
    notice_type: str = ""  # add_comment / add_reply
    is_mentioned: bool = False
    open_id: str = ""
    user_id: str = ""

    @property
    def key(self) -> str:
        """去重键。事件会重投，同一条评论不能回两遍。"""
        return f"{self.comment_id}:{self.reply_id}"


@dataclass
class EditPlan:
    """一次待确认的改动。block_id 为空且 append=True 表示追加到文末。"""

    block_id: str = ""
    old_text: str = ""
    new_text: str = ""
    append: bool = False
    why: str = ""


def parse_comment_event(payload: Dict[str, Any]) -> Optional[CommentEvent]:
    """长连接推来的 JSON → CommentEvent。不是评论事件返回 None。"""
    if not isinstance(payload, dict):
        return None
    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or "")
    if event_type and event_type != "drive.notice.comment_add_v1":
        return None
    event = payload.get("event") or {}
    meta = event.get("notice_meta") or {}
    from_user = meta.get("from_user_id") or {}
    file_token = str(meta.get("file_token") or "")
    comment_id = str(event.get("comment_id") or "")
    if not file_token or not comment_id:
        return None
    return CommentEvent(
        file_token=file_token,
        file_type=str(meta.get("file_type") or "docx"),
        comment_id=comment_id,
        reply_id=str(event.get("reply_id") or ""),
        notice_type=str(meta.get("notice_type") or ""),
        is_mentioned=bool(event.get("is_mentioned")),
        open_id=str(from_user.get("open_id") or ""),
        user_id=str(from_user.get("user_id") or ""),
    )


def authorized(open_id: str, allow: Sequence[str]) -> bool:
    """
    白名单之外一律不响应，且**不回话解释**。

    和私聊不同：这里的回复整篇文档的协作者都看得见，对陌生人回「你不在白名单」
    等于把这套东西广播出去，泄露面比私聊大得多。
    """
    allowed = {str(x).strip() for x in (allow or []) if str(x).strip()}
    return bool(open_id) and open_id in allowed


def pick_reply(replies: Sequence[Any], reply_id: str) -> Optional[Any]:
    """
    取事件对应的那条回复。

    评论串是一串回复，事件只给 reply_id。找不到就退回最后一条——重投或时序错乱时
    「最后一条」几乎总是刚发的那条，比什么都不回强。
    """
    items = list(replies or [])
    if not items:
        return None
    if reply_id:
        for r in items:
            if str(getattr(r, "reply_id", "") or "") == reply_id:
                return r
    return items[-1]


def pick_reply_text(replies: Sequence[Any], reply_id: str) -> str:
    item = pick_reply(replies, reply_id)
    return str(getattr(item, "text", "") or "").strip() if item is not None else ""


def pick_reply_id(replies: Sequence[Any], reply_id: str) -> str:
    """
    表情要贴到哪条回复上。云文档的表情挂在 **reply_id** 而不是 comment_id 上。

    不能直接用事件里的 reply_id：轮询兜底合成的事件、以及 add_comment 这类通知里
    它可能是空的。走和 pick_reply_text 同一套回退规则，免得正文读的是一条、
    表情贴到另一条上。
    """
    item = pick_reply(replies, reply_id)
    return str(getattr(item, "reply_id", "") or "") if item is not None else ""


def mentions_bot(text: str, trigger: str, self_open_id: str = "") -> bool:
    """
    评论里点名了才响应，否则同事之间的讨论也会被接上。

    必须同时认 open_id：评论接口返回的正文里，@ 是一个 person 元素，拼成文本时只有
    `@ou_xxx` 这串 id，**没有显示名**。只比对「@BloomBot」的话，用户明明 @ 了机器人，
    这里却判成没点名，评论被静悄悄丢掉——排查时最容易误判成「事件没收到」。
    """
    blob = (text or "").lower()
    needle = (trigger or "").strip().lstrip("@").lower()
    if needle and needle in blob:
        return True
    uid = (self_open_id or "").strip().lower()
    return bool(uid) and f"@{uid}" in blob


def strip_trigger(text: str, trigger: str) -> str:
    needle = (trigger or "").strip().lstrip("@")
    out = (text or "").strip()
    if needle:
        out = re.sub(r"@?" + re.escape(needle), " ", out, flags=re.IGNORECASE)
    # 顺手去掉飞书 @人 留下的 user_id 占位
    out = re.sub(r"@ou_\w+", " ", out)
    return re.sub(r"\s+", " ", out).strip(" ，,：:。")


def comment_question(instruction: str, quote: str) -> str:
    """
    这条评论存进记忆时用什么问法。

    不能直接用指令原话：划词评论里最常见的就是选中一段正文、只写一句「记一下」，
    存出来的记忆标题就是「记一下」，既检索不到也看不出讲什么。选中的那段正文才是
    这条记忆的主题，所以有 quote 就以它为准。

    指令本身有实质内容时（「这段的口径是什么」）两个都留：选中文字给主题，指令给角度。
    """
    from .intent import detect_remember

    topic = re.sub(r"\s+", " ", (quote or "").strip())
    body = (instruction or "").strip()
    if not topic:
        return body
    intent = detect_remember(body)
    # 「记一下」这类空洞指令没有信息量，接在主题后面只会污染问法
    if not body or (intent is not None and not intent.content):
        return topic
    return f"{topic}：{body}"


_EDIT_HINT_RE = re.compile(
    r"(改成|改为|换成|替换成|替换为|写成|调整为|修改为|删掉|删除|去掉|"
    r"补上|补充|加上|加一句|追加|订正|修正|应该是|应该写|建议改)"
)
# 「这里怎么改？」是提问不是指令：带疑问尾巴的一律走回答，宁可多问一句
_QUESTION_TAIL_RE = re.compile(r"(吗|呢|么)[?？]?$|[?？]$")


def classify(text: str) -> str:
    """"edit"（要改文档）还是 "ask"（要个答复）。拿不准一律当提问。"""
    t = (text or "").strip()
    if not t:
        return "ask"
    if _QUESTION_TAIL_RE.search(t):
        return "ask"
    return "edit" if _EDIT_HINT_RE.search(t) else "ask"


def _norm_short(text: str) -> str:
    return re.sub(r"[\s，。,.!！~、]+", "", (text or "")).lower()


_CONFIRM_WORDS = {
    "确认",
    "确认改",
    "确认吧",
    "确认修改",
    "确定",
    "确定改",
    "同意",
    "同意改",
    "同意修改",
    "可以",
    "可以改",
    "行",
    "行吧",
    "好",
    "好的",
    "改吧",
    "就这么改",
    "按你说的改",
    "没问题",
    "ok",
    "okay",
    "yes",
    "y",
    "go",
}
_CANCEL_WORDS = {
    "算了",
    "取消",
    "不改",
    "不改了",
    "先不改",
    "别改",
    "不用",
    "不用了",
    "放弃",
    "撤销",
    "no",
    "n",
}


def is_confirmation(text: str) -> bool:
    """
    只认明确表态，不做模糊匹配。

    「确认一下这个数对不对」也以「确认」开头——按前缀匹配就会把它当成放行，
    真去改了文档。宁可多问一句，也不能猜着落笔。
    """
    return _norm_short(text) in _CONFIRM_WORDS


def is_cancellation(text: str) -> bool:
    return _norm_short(text) in _CANCEL_WORDS


def _first_quoted(text: str) -> str:
    m = re.search(r"[「『“\"']([^「」『』“”\"']{1,120})[」』”\"']", text or "")
    return m.group(1).strip() if m else ""


_EDIT_VERB_RE = re.compile(r"(改成|改为|换成|替换成|替换为|写成|调整为|修改为)")


def extract_replacement(instruction: str, block_text: str) -> Tuple[str, str]:
    """
    从「把 A 改成 B」这类明确指令里算出整块的新文字。

    返回 (new_text, why)；算不出来返回 ("", "")，由调用方交给模型。
    只处理明确到不会有歧义的写法，含糊的宁可多走一趟模型也别猜。
    """
    text = (instruction or "").strip()
    m = _EDIT_VERB_RE.search(text)
    if not m:
        return "", ""
    new_part = text[m.end() :].strip().strip(_QUOTES + "：: 　")
    if not new_part:
        return "", ""
    old_part = _first_quoted(text[: m.start()])
    if old_part and old_part in (block_text or ""):
        return block_text.replace(old_part, new_part), f"把「{old_part}」换成「{new_part}」"
    return new_part, "整段改写"


_APPEND_RE = re.compile(r"(文末|末尾|最后|结尾|文档最后)[^，,。]{0,6}?(补充|加上|追加|加一段|写上|新增)")
_APPEND_SPLIT_RE = re.compile(r"(补充|加上|追加|加一段|写上|新增)")


def wants_append(instruction: str) -> bool:
    """「在文末补充…」是唯一不需要定位就能安全执行的改动：不动任何已有内容。"""
    return bool(_APPEND_RE.search(instruction or ""))


def extract_append_text(instruction: str) -> str:
    m = _APPEND_SPLIT_RE.search(instruction or "")
    if not m:
        return ""
    return (instruction[m.end() :]).strip().strip(_QUOTES + "：: 　")


def is_bot_reply(text: str) -> bool:
    """
    机器人自己发的回复不能再处理一遍。

    自动回复里就带着「BloomBot」四个字，而触发词判断是「正文里出现 @BloomBot」——
    少了这道闸，它会自己 @ 到自己，在评论串里无限刷下去。
    """
    return BOT_PREFIX.strip() in (text or "")


def looks_like_confirmation_attempt(text: str) -> bool:
    """短、且带表态意味，但没落在确认词表里——这种要提醒一句，不能装没看见。"""
    t = _norm_short(text)
    if not t or len(t) > 12:
        return False
    return any(word in t for word in ("确认", "确定", "同意", "可以", "改", "ok", "好"))


ANSWER_CONTRACT = (
    "你是团队的知识助手。下面是一条飞书文档评论里的问题，"
    "**你只需要写出答案正文**，调用方会把它发到评论区、也会负责写记忆。要求：\n"
    "1. 直接给结论，不要复述问题，不要客套\n"
    "2. 结合下面给出的记忆库参考与文档正文；两者矛盾时以文档正文为准并点明差异\n"
    "3. 不确定就说不确定，不要编造数字、链接、人名\n"
    "4. 控制在 300 字以内，评论框放不下长文\n"
    "5. **不要调用任何工具**：不要去发评论 / 改文档 / 写记忆，也不要回答"
    "「我不能代发评论」这类关于你自己权限的话——那不是提问者要的答案。"
    "你的输出会被原样当作答案，写成元回复就等于没回答"
)

EDIT_CONTRACT = (
    "你在改一段飞书文档正文。只输出改写后的这一段文字本身：\n"
    "1. 不要加解释、不要加引号、不要用 Markdown 包裹\n"
    "2. 保持原段落的语气与术语，只改指令要求改的部分\n"
    "3. 改动尽量小；指令没提到的内容原样保留"
)


def build_answer_context(
    *,
    doc_title: str = "",
    quote: str = "",
    doc_text: str = "",
    references: Sequence[Dict[str, Any]] = (),
) -> str:
    """拼给模型的上下文：记忆库参考在前、文档正文在后，正文更新所以更可信。"""
    parts: List[str] = []
    if references:
        lines = []
        for i, ref in enumerate(references, 1):
            q = str(ref.get("question") or "").strip()
            a = str(ref.get("answer") or "").strip()
            lines.append(f"{i}. {q}\n   {a[:400]}")
        parts.append("【记忆库参考（可能过时，与正文冲突时以正文为准）】\n" + "\n".join(lines))
    if doc_title:
        parts.append(f"【文档标题】{doc_title}")
    if quote:
        parts.append(f"【评论选中的原文】{quote}")
    if doc_text:
        parts.append("【文档正文（截断）】\n" + doc_text[:MAX_DOC_CHARS])
    return "\n\n".join(parts)


def build_edit_prompt(instruction: str, block_text: str, *, quote: str = "") -> str:
    return (
        f"{EDIT_CONTRACT}\n\n"
        f"【原文】\n{block_text}\n\n"
        + (f"【评论选中的部分】{quote}\n\n" if quote else "")
        + f"【改动要求】\n{instruction}\n\n改写后的段落："
    )


def clean_model_text(raw: str) -> str:
    """模型爱裹 ``` 和引号，落进文档里就是脏字符。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text.strip().strip(_QUOTES).strip()


def clip_comment(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_COMMENT_CHARS:
        return text
    return text[:MAX_COMMENT_CHARS].rstrip() + "…（完整内容在 BloomBox 记忆库里）"


# ---------- 回复文案 ----------


def format_answer(text: str) -> str:
    return f"{BOT_PREFIX}\n{clip_comment(text)}"


def format_ack() -> str:
    return f"{BOT_PREFIX}\n收到，正在查记忆库并思考，稍等一会儿。"


def format_cannot(reason: str) -> str:
    return f"{BOT_PREFIX}\n没动手：{reason}"


def format_proposal(plan: EditPlan) -> str:
    """提案：把改动摊开讲清楚，等本人回「确认」。绝不在这一步落笔。"""
    if plan.append:
        body = f"打算在文末追加一段（现有内容一个字不动）：\n{clip_comment(plan.new_text)}"
    else:
        body = (
            f"打算改这一段：\n原文：{clip_comment(plan.old_text)}\n"
            f"改成：{clip_comment(plan.new_text)}"
        )
    why = f"\n理由：{plan.why}" if plan.why else ""
    return (
        f"{BOT_PREFIX}\n{body}{why}\n\n"
        "还没有改。回一句「确认」我就改；不想改回「算了」。"
    )


def format_applied(plan: EditPlan) -> str:
    where = "文末新增一段" if plan.append else f"改了一段（block {plan.block_id}）"
    return (
        f"{BOT_PREFIX}\n已改：{where}。\n"
        f"现在是：{clip_comment(plan.new_text)}\n"
        "改错了可以在飞书右上角「…」→ 历史版本 里回滚。"
    )


def format_cancelled() -> str:
    return f"{BOT_PREFIX}\n好，不改了，提案已作废。"


def format_needs_confirmation() -> str:
    return (
        f"{BOT_PREFIX}\n上一条提案还等着你表态：回「确认」我就改，回「算了」就作废。\n"
        "要换个改法的话，重新 @ 我一次并说清楚改成什么。"
    )
