"""判断一句话是不是「让我记下来」，以及要记的是哪一段。

以前两边各写各的：飞书机器人只认句首的 `记一下`，沙箱只认 `记住：`，
于是「这个结论记一下」「把飞书长连接的坑存到记忆库」这类正常说法一句都不认。
口径统一放这里，改判定只改这个文件。

刻意只用标准库、不碰网络：识别是纯字符串判断，要能脱离飞书和 HTTP 跑单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .utils import LONG_TERM_RECORD_MARKER

# 「这个结论」「上面这条」只是指代，本身没有内容，得靠引用或上一轮回答补
_DEICTIC = (
    r"(?:(?:这|那|此|它|上面|上边|前面|以上|刚才|刚刚)(?:个|条|段|则|些|里|边)?的?"
    r"(?:结论|内容|消息|信息|东西|回复|答复|答案|说明|分析|卡片|报告|问题|事|话)?"
    r"|\bthis\b|\bthat\b|\bit\b|\bthe above\b)"
)
_DEICTIC_ONLY_RE = re.compile(r"^(?:" + _DEICTIC + r")+$", re.IGNORECASE)

_POLITE = r"(?:麻烦|请|帮我|帮忙|给我|顺便|另外|你|再)*"
# 「这个**一定要**记住」：指代和动词之间常夹情态词
_MODAL = r"(?:要|得|需要|务必|一定|最好|必须)*"
# 「…，记一下，别忘了」「记一下这个，谢谢」：句尾的客套不是要记的内容
_FILLER = r"(?:别忘了?|记得|谢谢|多谢|thanks?|thx)"
_TAIL_FILLER = r"(?:[，,。、\s]*" + _FILLER + r")*"
_TRAILING_FILLER_RE = re.compile(_TAIL_FILLER + r"[。！!~，,\s]*$", re.IGNORECASE)
_MEM = r"(?:长期记忆|长时记忆|记忆库|记忆里|记忆中|记忆)"
_INTO_MEM = r"(?:\s*(?:到|进|入|至)\s*" + _MEM + r")"

# 强动词自带祈使语气，后面可以直接跟内容：「记住长连接怎么保存」
_VERB_STRONG = r"(?:记一下|记一记|记下来|记下|记住|记录一下|存一下|存下来|存起来)"
# 弱动词兼作名词，「保存位置在哪」不是要记东西，所以后面必须跟停顿、指代或「到记忆库」
_VERB_WEAK = r"(?:记录|保存|收藏|备忘|存)"
# 「的/了/过」跟在动词后面就是在陈述或提问，不是在下指令
_NOT_PARTICLE = r"(?![的了过着地得])"
_WEAK_BOUNDARY = r"(?=[\s:：，,、。]|" + _DEICTIC + r"|$)"

_VERB = (
    r"(?:"
    + _VERB_STRONG + _NOT_PARTICLE + _INTO_MEM + r"?"
    + r"|" + _VERB_WEAK + _NOT_PARTICLE + _INTO_MEM + r"?" + _WEAK_BOUNDARY
    + r"|(?:加|写|放|存|记|丢|塞|同步)" + _INTO_MEM
    + r")"
)

# 动词在前：「记一下：xxx」「帮我记住 xxx」「存到记忆库」
_HEAD_RE = re.compile(
    r"^" + _POLITE + r"(?:把|将)?\s*(?:" + _DEICTIC + r")?\s*" + _MODAL + r"\s*"
    + _VERB
    + r"\s*[:：，,、\-—]*\s*(?P<body>[\s\S]*)$"
)
# 「把 xxx 记一下」：有「把/将」兜底，中间是什么都不会误伤
_BA_RE = re.compile(
    r"^" + _POLITE + r"(?:把|将)\s*(?P<body>[\s\S]{1,300}?)\s*" + _VERB + r"\s*[。！!~]*$"
)
# 动词在后：「这个报警的根因是 xxx，记一下」。必须有停顿，否则「这功能怎么记录」也会中
_TAIL_RE = re.compile(
    r"^(?P<body>[\s\S]+?)\s*[，,；;。\n]\s*" + _POLITE + r"(?:把|将)?\s*"
    r"(?:" + _DEICTIC + r")?\s*" + _MODAL + r"\s*" + _VERB
    + r"\s*[吧呀啊哈嘛了]*" + _TAIL_FILLER + r"[。！!~，,\s]*$"
)
_EN_RE = re.compile(
    r"^(?:please\s+)?(?:remember|memorize|jot\s+down|note\s+down)\b"
    r"(?:\s+(?:that|this|it|the\s+above))?\s*[:\-—,]?\s*(?P<body>[\s\S]*)$",
    re.IGNORECASE,
)

_TRAILING_DOTS_RE = re.compile(r"[。．.!\s]+$")
_QA_SPLIT_RE = re.compile(r"^(?P<q>.+?)\s*(?:=>|→|->|＝|=|\|\|)\s*(?P<a>.+)$", re.DOTALL)


def is_deictic(text: str) -> bool:
    """这段话是不是只有指代、没有实质内容。"""
    stripped = (text or "").strip().strip("，,。.！!？?：: 　")
    return bool(stripped) and bool(_DEICTIC_ONLY_RE.match(stripped))


@dataclass
class RememberIntent:
    """要记东西。内容可能得靠上下文补——「记一下这个结论」本身没说要记什么。"""

    body: str

    @property
    def content(self) -> str:
        """真正要记的那段；只有指代（「这个结论」）时是空串。"""
        text = (self.body or "").strip()
        return "" if is_deictic(text) else text


def detect_remember(text: str) -> Optional[RememberIntent]:
    """认出写入意图；只是提问或闲聊返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    # 「…，记录到长期记忆。」是检索用的拼接标记（见 assemble_long_term_query），
    # 不是写入指令。不在这里拦住，每一次 MCP 检索都会顺手写一条库。
    if _TRAILING_DOTS_RE.sub("", raw).endswith(LONG_TERM_RECORD_MARKER):
        return None
    # 问号结尾是在问，不是在交代
    if raw.endswith(("?", "？")):
        return None
    for pattern in (_HEAD_RE, _BA_RE, _TAIL_RE, _EN_RE):
        matched = pattern.match(raw)
        if matched:
            body = _TRAILING_FILLER_RE.sub("", (matched.group("body") or "").strip())
            return RememberIntent(body=body.strip())
    return None


def split_question_answer(body: str) -> Optional[Tuple[str, str]]:
    """用户自己把问和答都给了就拆开；只有一坨内容返回 None。"""
    text = (body or "").strip()
    if not text:
        return None
    lines = text.split("\n")
    head, rest = lines[0].strip(), "\n".join(lines[1:]).strip()
    if head and rest:
        return head, rest
    matched = _QA_SPLIT_RE.match(head)
    if matched:
        question, answer = matched.group("q").strip(), matched.group("a").strip()
        if question and answer:
            return question, answer
    return None
