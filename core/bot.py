"""飞书机器人的纯逻辑层：事件 JSON → 回复文本。

刻意不 import lark_oapi、不发任何网络请求。长连接与发消息在 feishu_bot.py，
这样鉴权、去重、命令解析这些真正容易出错的地方都能脱离飞书环境跑单测。
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

from .intent import detect_remember, is_deictic, split_question_answer

# 群里 @人 在正文里是占位符（@_user_1 / @_all），不去掉会一起进检索
MENTION_RE = re.compile(r"@_(?:user_\w+|all)\b")

STATUS_WORDS = {"状态", "记忆状态", "status"}
# 问机器人自己是干嘛的，直接给说明书；绕一圈模型既慢又答不准
HELP_WORDS = {
    "帮助",
    "help",
    "?",
    "？",
    "怎么用",
    "用法",
    "功能",
    "有什么功能",
    "你有什么功能",
    "能做什么",
    "你能做什么",
    "你会什么",
    "你是谁",
}

# 飞书单条文本消息很长也能发，但聊天窗口里刷屏没法看；超出的让用户回 BloomBox 看
MAX_REPLY_CHARS = 1800
MAX_REF_ANSWER_CHARS = 200
# 引用的卡片可能很长，全塞进一条记忆既难检索也难读
MAX_QUOTED_CHARS = 4000
# 群里捞来的上下文交给模型的预算：单条与总量。
# 给得比看着需要的宽：报警卡片带上链接就有四千字，卡在一千五会把关键证据和链接一起截掉，
# 而这段只进模型的 prompt、不进记忆库，长一点不占地方
MAX_CONTEXT_MESSAGE_CHARS = 6000
MAX_CONTEXT_CHARS = 10000

HELP_TEXT = """记忆沙箱机器人，直接说话就是查记忆。

· 查记忆：把问题发给我，例如「客服技术文档在哪」
· 记记忆：说人话就行，「记一下…」「把这个存到记忆库」「…，记下来」都认
    记一下：飞书长连接怎么保存
    先把本地 bot 跑起来，再回后台点保存，否则保存失败
  （第一行当问题，后面几行当答案；单行可用 => 或 || 分隔）
· 记别人说的：回复那条消息，再说「记一下这个结论」
  （正文取被引用的那条；想自定义问法就写「记一下 xxx 的根因」）
· 看状态：状态
· 这段说明：帮助

查的写的都是同一个记忆库。"""

# 别的应用发的卡片，接口只给一个带 image_key 的摘要壳。翻上游也凑不出结论时的出路。
# 转发这条放在最前面：实测跨应用卡片卡的是 sender_type=app，同一张卡片由人转发一次
# （sender_type=user）就能拿到完整正文，比让人手动复制一大段省事得多
CANNOT_READ_CARD = (
    "这张卡片是别的应用发的，飞书不把正文给我。两条路：\n"
    "1）把它转发一次（转给我或转到群里），转发后发送人变成你，飞书就给全文了，"
    "再回复你转发的那条说「记下来」；\n"
    "2）把卡片里的结论选中复制发我，例如「记一下 xxx 的根因：…」。"
)


@dataclass
class Incoming:
    """一条已经归一化的用户消息。"""

    message_id: str
    chat_id: str
    chat_type: str  # p2p / group
    open_id: str
    text: str
    parent_id: str = ""  # 用户回复的那条消息，正文得另外拉
    # 这条 @ 了谁（open_id 与显示名）。群里要靠它判断这句到底是不是说给自己听的
    mention_ids: Tuple[str, ...] = ()
    mention_names: Tuple[str, ...] = ()
    # 发送人是另一个机器人（Slardar / Mira 这类）。没权限时不该冲它喊白名单，闷声不理
    from_bot: bool = False


@dataclass
class Outgoing:
    reply_to: str  # 回复哪条消息（message_id）
    text: str
    # 本地没命中、还值得再交给模型算一轮。填的是用户原话，`text` 是模型跑不动时的兜底话术。
    # 慢通道要几十秒，长连接回调等不起，所以只在这里挂个标记，由调用方决定怎么排队。
    slow_query: str = ""
    # 有值时慢通道走的是另一件事：拿这段群聊上下文提炼结论并写进记忆库，
    # 而不是把 slow_query 当问题去检索。见 remember_with_context
    slow_context: str = ""


@dataclass
class Command:
    kind: str  # ask / remember / status / help
    query: str = ""
    question: str = ""
    answer: str = ""
    from_quote: bool = False  # 答案来自被引用的消息，回执里要说清楚


class SeenMessages:
    """事件可能重投，同一条消息不能回两遍。"""

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = max(1, capacity)
        self._order: Deque[str] = deque()
        self._ids: Set[str] = set()

    def check_and_add(self, message_id: str) -> bool:
        """第一次见到返回 True；重复返回 False。"""
        if not message_id:
            return True
        if message_id in self._ids:
            return False
        self._ids.add(message_id)
        self._order.append(message_id)
        while len(self._order) > self._capacity:
            self._ids.discard(self._order.popleft())
        return True


def _node_link(node: Dict[str, Any]) -> str:
    """一个节点上挂的跳转地址。报警卡片的「报警详情」「下钻分析」都在这上面。"""
    for key in ("href", "url"):
        value = node.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            # 多端链接：{"urlVal": {"url": ..., "pc_url": ...}}，取通用的那个
            inner = value.get("url") or (value.get("urlVal") or {}).get("url")
            if isinstance(inner, str) and inner.startswith("http"):
                return inner
    return ""


def _linked(text: str, link: str) -> str:
    """带链接的文案写成 Markdown。丢了链接，记下来的结论就没法回原地复查。"""
    if not link:
        return text
    return f"[{text}]({link})" if text else link


def _post_text(content: Dict[str, Any]) -> str:
    """富文本（post）：把所有 text 段落拼起来，忽略图片/表情，但保留链接。"""
    lines: List[str] = []
    title = (content.get("title") or "").strip()
    if title:
        lines.append(title)
    for paragraph in content.get("content") or []:
        if not isinstance(paragraph, list):
            continue
        parts = [
            _linked(str(node.get("text") or ""), _node_link(node))
            for node in paragraph
            if isinstance(node, dict) and node.get("tag") in ("text", "a", "md")
        ]
        lines.append("".join(parts))
    return "\n".join(lines)


def _card_text(content: Dict[str, Any]) -> str:
    """卡片（interactive）：递归捞出可见文案，带上链接。

    卡片的 schema 版本、嵌套结构（column_set / note / action）花样太多，按 tag 逐个适配
    早晚会漏，所以只认「哪些字段是给人看的字符串」。

    链接必须一起捞：报警卡片里的「报警详情」「下钻分析」全靠 href，只取 text 的话
    存进记忆的结论就是个断头路，回头没法点回原地复查。
    """
    lines: List[str] = []
    # 报警卡片常把同一行在摘要区和明细区各写一遍，带上链接后长度直接翻倍。
    # 全局去重（不只是相邻）能砍掉一半，重复的那份也没带新信息
    seen: Set[str] = set()

    def emit(line: str) -> None:
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "img":
                return
            link = _node_link(node)
            emitted = False
            for key in ("content", "text"):
                value = node.get(key)
                if isinstance(value, str):
                    text = value.strip()
                    if text:
                        emit(_linked(text, link))
                        emitted = True
            if link and not emitted:
                emit(link)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(content)
    return "\n".join(lines)


def message_text(message_type: str, content_raw: str) -> Optional[str]:
    """取纯文本；取不出文字的消息类型返回 None。"""
    try:
        content = json.loads(content_raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(content, dict):
        return None
    if message_type == "text":
        raw = str(content.get("text") or "")
    elif message_type == "post":
        raw = _post_text(content)
    elif message_type == "interactive":
        # 报警、日报这类值得记的东西恰好都是卡片
        raw = _card_text(content)
    else:
        return None
    return MENTION_RE.sub(" ", raw).strip()


def parse_event(payload: Dict[str, Any], *, self_open_id: str = "") -> Optional[Incoming]:
    """
    im.message.receive_v1 事件 → Incoming；不该处理的返回 None。

    别的机器人 @ 过来是要接的（Slardar 报警、Mira 结论可以直接驱动落库，不必人转一手），
    但要开通「获取群组中其他机器人和用户@当前机器人的消息」才收得到这类事件。

    自己发的消息也会回推，接了就死循环，所以只在**认得出自己**且发送人确实是别人时才放行：
    `self_open_id` 没传、或对方 open_id 取不到，一律按老规矩丢掉。宁可漏接，不能自问自答。
    """
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    sender = event.get("sender") or {}
    sender_open_id = str((sender.get("sender_id") or {}).get("open_id") or "")
    from_bot = (sender.get("sender_type") or "user") != "user"
    if from_bot:
        me = (self_open_id or "").strip()
        if not me or not sender_open_id or sender_open_id == me:
            return None
    message = event.get("message") or {}
    text = message_text(
        str(message.get("message_type") or ""), str(message.get("content") or "")
    )
    if not text:
        return None
    ids: List[str] = []
    names: List[str] = []
    for m in message.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        oid = str((m.get("id") or {}).get("open_id") or "")
        if oid:
            ids.append(oid)
        name = str(m.get("name") or "").strip()
        if name:
            names.append(name)
    return Incoming(
        message_id=str(message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        open_id=sender_open_id,
        text=text,
        parent_id=str(message.get("parent_id") or message.get("root_id") or ""),
        mention_ids=tuple(ids),
        mention_names=tuple(names),
        from_bot=from_bot,
    )


def addressed_to_me(incoming: Incoming, *, self_open_id: str, self_name: str = "") -> bool:
    """
    群里这句话是不是说给自己听的。

    群消息不做这层判断，机器人就会去接别人 @ 另一个机器人的话——真发生过：
    用户 @ 的是运维助理，BloomBot 也跟着答了一条。单聊不受影响，那本来就是对着自己说的。

    认不出自己（open_id 没取到、名字也没配）时返回 True：宁可多嘴，也好过整个群里
    突然全哑，后者没有任何错误信息，只会被当成机器人挂了。
    """
    if incoming.chat_type != "group":
        return True
    me = (self_open_id or "").strip()
    name = (self_name or "").strip()
    if not me and not name:
        return True
    if me and me in incoming.mention_ids:
        return True
    return bool(name) and name in incoming.mention_names


def authorize(open_id: str, allow: Sequence[str]) -> Optional[str]:
    """有权限返回 None，否则返回要回给对方的话（绝不带任何记忆内容）。"""
    allowed = [x.strip() for x in (allow or []) if str(x).strip()]
    if not allowed:
        # 先有鸡还是先有蛋：用户不知道自己 open_id，就没法配白名单。
        # 未配置时一律不服务，只把 open_id 告诉他。
        return (
            "机器人还没有配置白名单，暂不提供服务。\n"
            f"你的 open_id：{open_id or '(未取到)'}\n"
            "把它加到 config.yaml 的 feishu.bot_allow_open_ids，然后重启机器人。"
        )
    if open_id not in allowed:
        return "抱歉，这个机器人只对指定成员开放。"
    return None


def _quoted_question(body: str, quoted: str) -> str:
    """「记一下这个结论」这种指代没法当问法，退回用引用正文的第一行。"""
    label = body.strip().strip("，,。.！!？?：: 　")
    if label and not is_deictic(label):
        return _clip(label, 80)
    for line in quoted.split("\n"):
        if line.strip():
            return _clip(line.strip(), 80)
    return _clip(body.strip(), 80)


def parse_command(text: str, quoted: str = "") -> Command:
    stripped = text.strip()
    # 「你有什么功能？」带着问号就落进检索了，先把尾巴上的标点去掉再比
    probe = stripped.lower().rstrip("？?。.！!~ 　") or stripped.lower()
    if probe in HELP_WORDS:
        return Command(kind="help")
    if probe in STATUS_WORDS:
        return Command(kind="status")

    intent = detect_remember(stripped)
    if intent is None:
        return Command(kind="ask", query=stripped)

    # 用户自己把问题和答案都给全了，引用就不掺和
    pair = split_question_answer(intent.body)
    if pair:
        return Command(kind="remember", question=pair[0], answer=pair[1])

    quoted = quoted.strip()
    if quoted:
        return Command(
            kind="remember",
            question=_quoted_question(intent.body, quoted),
            answer=_clip(quoted, MAX_QUOTED_CHARS),
            from_quote=True,
        )
    if intent.content:
        # 一句话也照存：问答同文照样能检索到，比逼用户重打一遍格式强
        return Command(kind="remember", question=intent.content, answer=intent.content)
    # 只有一个「这个」，又没引用任何消息，实在不知道要记什么
    return Command(kind="help")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _clip_reply(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[:MAX_REPLY_CHARS].rstrip() + "\n…（内容较长，完整版在 BloomBox 里看）"


def format_references(references: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for i, ref in enumerate(references, 1):
        question = _clip(str(ref.get("question") or ""), 60)
        answer = _clip(str(ref.get("answer") or ""), MAX_REF_ANSWER_CHARS)
        lines.append(f"{i}. {question}\n   {answer}")
    return "\n".join(lines)


def _hit_ids(result: Any) -> set:
    meta = getattr(result, "meta", None) or {}
    return {str(h.get("id")) for h in (meta.get("hits") or []) if h.get("id")}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _drop_the_answer_itself(
    references: Sequence[Dict[str, Any]], answer: str, hit_ids: set
) -> List[Dict[str, Any]]:
    """
    软召回用的是同一个 query，硬命中的那条必然也在里面，而且分数最高。
    不摘掉它，回复就变成「先给答案，再把同一段话原样抄一遍」——用户看到的是答了两次。

    长时命中能拿到 id，直接按 id 排除；工作/程序性记忆命中时没有 id，只能比内容。
    """
    body = _squash(answer)
    kept: List[Dict[str, Any]] = []
    for ref in references:
        if str(ref.get("id") or "") in hit_ids:
            continue
        ref_answer = _squash(str(ref.get("answer") or ""))
        # 多条命中时答案被拼成「[1] … [2] …」，所以是包含而不是相等。
        # 限一个长度下限：短答案（"3001"）碰巧被包含的多半是另一条记忆，不能误删
        if ref_answer and (ref_answer == body or (len(ref_answer) >= 20 and ref_answer in body)):
            continue
        kept.append(ref)
    return kept


def _do_ask(sb: Any, query: str) -> Tuple[str, bool]:
    """返回 (回复文本, 是否硬命中)。没命中时文本只是兜底，调用方可以改走模型。"""
    from .utils import assemble_long_term_query

    assembled = assemble_long_term_query(query)
    result = sb.ask_local(assembled)
    hit = result.source not in ("miss", "sensory_reject", "llm")
    if not hit and assembled != query:
        # 兼容库里未带「记录到长期记忆」后缀的旧条目
        result = sb.ask_local(query)
        hit = result.source not in ("miss", "sensory_reject", "llm")

    parts: List[str] = []
    answer = result.answer.strip() if hit else ""
    if hit:
        parts.append(answer)
    # 多要一条：摘掉命中的那条之后还能剩下 3 条真·相关的
    pack = sb.build_reference_pack(query, top_k=4 if hit else 3)
    references = pack.get("references") or []
    if hit:
        references = _drop_the_answer_itself(references, answer, _hit_ids(result))[:3]
    if references:
        parts.append(("相关记忆：" if hit else "没有直接命中，相关的有：") + "\n" + format_references(references))
    elif not hit:
        parts.append("本地记忆里没找到相关内容。想让我记住的话，说一句「记一下 …」就行。")
    return "\n\n".join(p for p in parts if p), hit


def llm_available(sb: Any) -> bool:
    """沙箱配了模型才值得把没命中的问题挪到慢通道；没配就维持原来的「没找到」。"""
    return getattr(sb, "llm", None) is not None


def answer_with_llm(sb: Any, query: str) -> str:
    """
    慢通道：本地没命中，交给沙箱配的模型/本机 agent 现算一个结论。

    `sb.chat()` 会自己再走一遍本地检索（毫秒级）再回退模型，命中就不会真去跑 agent；
    答出来的东西也由它负责写回记忆库，这里只管把话说清楚。
    """
    try:
        result = sb.chat(query)
    except Exception as exc:  # noqa: BLE001 - agent 超时/没装是常态，原样告诉用户
        return _clip_reply(f"问模型失败：{exc}")

    text = (getattr(result, "answer", "") or "").strip()
    if not text:
        return "模型没返回内容，本地记忆里也没有相关记录。"

    from .working import is_non_reusable_answer

    if not is_non_reusable_answer(text):
        # 与 sb.chat() 的写回条件一致：能复用的才真进了库，别许空愿
        text += "\n\n—— 本地记忆没命中，以上是模型现给的结论，已存进记忆库"
    return _clip_reply(text)


SENDER_LABELS = {"user": "群成员", "app": "机器人"}
# 跨应用卡片的摘要壳有时不是空的，而是这句占位提示。它比空还坏：抽得出字，
# 于是一路混进上下文，看着像内容
CARD_PLACEHOLDER = "请升级至最新版本客户端"

CONTEXT_REMEMBER_CONTRACT = (
    "下面是一段飞书群聊记录（按时间正序）。用户回复了群里某条消息并要你记下来，"
    "但那条消息是别的应用发的卡片，接口读不到正文，只能从这段上下文里还原。\n"
    "请提炼成一条可复用的长期记忆，严格按下面两行输出，不要加别的话：\n"
    "问题：<一句话问法，40 字以内，带上具体对象，别写「这个告警」这类指代>\n"
    "答案：<结论本身：是什么问题、关键证据、根因或处置办法，400 字以内（链接不计）>\n"
    "答案末尾附上原文里的关键链接（报警详情、下钻分析、日志、文档），"
    "照 [文字](链接) 原样抄，一个字符都别改——丢了链接，这条记忆就没法回原地复查；"
    "同一个页面的十几条下钻链接挑一两条代表性的即可。\n"
    "上下文里信息不足以得出结论时，答案只写「信息不足」四个字，不要编。"
)


def build_chat_context(messages: Sequence[Dict[str, str]]) -> str:
    """
    群历史 → 交给模型的上下文。读不出字的（跨应用卡片、图片、语音）直接丢掉。

    超预算时先丢**最短**的：「有结果了吗」「收到」这类占位置不带信息，
    而真正有料的告警卡片、日志往往就是最长的那条。丢完再按时间正序拼回去。
    """
    items: List[Tuple[int, str]] = []
    for i, msg in enumerate(messages or []):
        text = message_text(str(msg.get("msg_type") or ""), str(msg.get("content") or ""))
        if not text or not text.strip() or CARD_PLACEHOLDER in text:
            continue
        label = SENDER_LABELS.get(str(msg.get("sender_type") or ""), "未知")
        items.append((i, f"【{label}】{_clip(text, MAX_CONTEXT_MESSAGE_CHARS)}"))

    kept: List[Tuple[int, str]] = []
    used = 0
    for order, line in sorted(items, key=lambda x: len(x[1]), reverse=True):
        if used + len(line) > MAX_CONTEXT_CHARS:
            continue
        used += len(line)
        kept.append((order, line))
    return "\n".join(line for _order, line in sorted(kept))


def parse_context_qa(raw: str) -> Tuple[str, str]:
    """模型输出的「问题：/ 答案：」两行 → (问题, 答案)；格式没守住就返回空。"""
    text = (raw or "").strip()
    if not text:
        return "", ""
    question, answer, current = "", [], ""
    for line in text.splitlines():
        head = line.strip()
        if head.startswith(("问题：", "问题:")):
            question = head.split("：", 1)[-1].split(":", 1)[-1].strip()
            current = "q"
        elif head.startswith(("答案：", "答案:")):
            answer.append(head.split("：", 1)[-1].split(":", 1)[-1].strip())
            current = "a"
        elif current == "a":
            answer.append(line.rstrip())
    body = "\n".join(answer).strip()
    if not question or not body or body in ("信息不足", "信息不足。"):
        return "", ""
    return question, body


def remember_with_context(sb: Any, user_text: str, context: str) -> str:
    """
    慢通道之二：被引用的卡片读不出字，改用群里上下文提炼结论并落库。

    为什么不直接把上下文丢给 `sb.chat()`：那样问法就是整段聊天记录，写进库以后
    既检索不到也没法看。这里先让模型吐「问题 / 答案」两行，再照常 remember。
    """
    llm = getattr(sb, "llm", None)
    if llm is None:
        return CANNOT_READ_CARD

    prompt = f"{CONTEXT_REMEMBER_CONTRACT}\n\n【用户这轮说的】{(user_text or '').strip()}"
    try:
        raw = llm.generate(prompt, context=context)
    except Exception as exc:  # noqa: BLE001 - agent 超时/没装是常态，原样告诉用户
        return _clip_reply(f"想从群里的上下文还原结论，但模型没跑通：{exc}\n{CANNOT_READ_CARD}")

    question, answer = parse_context_qa(raw)
    if not question:
        return (
            "你回复的那条是别的应用发的卡片，接口读不到正文；"
            "我翻了这个话题的上游消息，也没凑出一个能存的结论。\n" + CANNOT_READ_CARD
        )
    try:
        message = sb.remember(question, answer, scene="dev")
    except Exception as exc:  # noqa: BLE001
        return _clip_reply(f"提炼出了结论但没存进去：{exc}")
    return _clip_reply(
        f"已记住：{_clip(question, 80)}\n"
        "（那张卡片接口读不到正文，这条是照着群里上游的消息还原的，记错了就回我一句改）\n"
        f"{message}"
    )


def _do_remember(sb: Any, cmd: Command) -> str:
    message = sb.remember(cmd.question, cmd.answer, scene="dev")
    updated = bool(getattr(sb, "last_remembered_updated", False))
    record = getattr(sb, "last_remembered", None)
    stored = getattr(record, "question", None) or cmd.question
    head = "已更新记忆：" if updated else "已记住："
    tail = "（正文取自你回复的那条消息）\n" if cmd.from_quote else ""
    return f"{head}{_clip(stored, 80)}\n{tail}{message}"


def _do_status(sb: Any) -> str:
    status = sb.status() or {}
    long_term = status.get("long_term") or {}
    working = status.get("working") or {}
    return (
        f"长时记忆 {long_term.get('declarative_count', '?')} 条"
        f"（程序性 {long_term.get('procedural_count', '?')} 条）\n"
        f"工作记忆 {working.get('size', '?')}/{working.get('max_size', '?')}"
        f"，当前场景 {working.get('scene') or '-'}"
    )


FetchMessage = Callable[[str], Optional[Tuple[str, str]]]
# (chat_id, 被引用消息的 message_id) → 它之前那几条的原始载荷，按时间正序
FetchContext = Callable[[str, str], Sequence[Dict[str, str]]]
# 「这条我接了」的回调，参数是用户那条消息的 message_id
StartCallback = Callable[[str], None]


def _quoted_text(fetch: FetchMessage, message_id: str) -> Tuple[str, str, str]:
    """拉被引用消息的正文，返回 (正文, 错误, 消息类型)。前两者都可能为空。"""
    try:
        got = fetch(message_id)
    except Exception as exc:  # noqa: BLE001 - 权限没开是常态，得把原因说给用户
        return "", str(exc), ""
    if not got:
        return "", "", ""
    return (message_text(got[0], got[1]) or ""), "", (got[0] or "")


def _chat_context(fetch: Optional[FetchContext], incoming: Incoming) -> Tuple[str, str]:
    """引用读不出字时的备用料：上游几条群消息。返回 (上下文, 错误)。"""
    if fetch is None or not incoming.chat_id:
        return "", ""
    try:
        return build_chat_context(fetch(incoming.chat_id, incoming.parent_id)), ""
    except Exception as exc:  # noqa: BLE001 - 缺 im:message.group_msg 时会一直失败，要说出来
        return "", str(exc)


def respond(
    sb: Any,
    payload: Dict[str, Any],
    *,
    allow: Sequence[str],
    seen: Optional[SeenMessages] = None,
    fetch_message: Optional[FetchMessage] = None,
    fetch_context: Optional[FetchContext] = None,
    on_start: Optional[StartCallback] = None,
    self_open_id: str = "",
    self_name: str = "",
) -> Optional[Outgoing]:
    """
    事件 → 该回什么。返回 None 表示这条不理。

    `on_start` 在「确定要干活了」之后、真正开干之前调一次，用来贴「处理中」表情。
    被忽略的消息（自己发的、重投的、没权限的、群里没 @ 到自己的）不会触发它。

    别的机器人 @ 过来也会走完整流程，但同样要过白名单：把那个机器人的 open_id
    配进 `allow` 才理它，否则静默丢弃（不回白名单话术，见下）。

    `fetch_context` 只在「引用的那条读不出字」时才用得上，去上游捞几条可读的当料。

    `self_open_id` / `self_name` 是机器人自己的身份，用来判断群消息是不是冲自己来的。
    """
    incoming = parse_event(payload, self_open_id=self_open_id)
    if incoming is None:
        return None
    if not addressed_to_me(incoming, self_open_id=self_open_id, self_name=self_name):
        return None
    if seen is not None and not seen.check_and_add(incoming.message_id):
        return None

    refusal = authorize(incoming.open_id, allow)
    if refusal:
        # 白名单话术是说给人听的（告诉他自己的 open_id 好去配置）。冲着另一个机器人喊，
        # 群里只会多一条没人看的噪音，对方也不会去配白名单
        if incoming.from_bot:
            return None
        return Outgoing(reply_to=incoming.message_id, text=refusal)

    if on_start is not None:
        try:
            on_start(incoming.message_id)
        except Exception:  # noqa: BLE001 - 表情是锦上添花，不能连带把回复吞了
            pass

    # 别的进程（BloomBox / MCP / CLI）可能刚写过盘
    try:
        sb.long_term.reload()
    except Exception:  # noqa: BLE001 - 重载失败也要能答，读到旧数据好过不回话
        pass

    quoted, quote_error = "", ""
    # 只有写入意图才值得多打一次接口
    wanted_quote = bool(
        fetch_message and incoming.parent_id and detect_remember(incoming.text) is not None
    )
    quoted_kind = ""
    if wanted_quote:
        quoted, quote_error, quoted_kind = _quoted_text(fetch_message, incoming.parent_id)

    cmd = parse_command(incoming.text, quoted=quoted)
    # 「记下来」+ 引用，正文却没到手。别急着回帮助文本：料往往就在上游几条里
    if cmd.kind == "help" and wanted_quote and not quoted:
        context, context_error = "", ""
        if llm_available(sb):
            context, context_error = _chat_context(fetch_context, incoming)
        if context:
            # 走慢通道：读上下文、提炼、落库都要几十秒，长连接回调等不起
            return Outgoing(
                reply_to=incoming.message_id,
                text=CANNOT_READ_CARD,  # 队列满了退回来时至少给条出路
                slow_query=incoming.text,
                slow_context=context,
            )
        if quote_error:
            text = f"读不到你回复的那条消息，所以没敢乱记：{quote_error}"
        elif quoted_kind == "interactive":
            # 别的应用发的卡片，接口只给一个带 image_key 的摘要壳，正文一个字都没有。
            # 实测应用身份与用户身份返回的字节数完全相同，不是权限问题，也没有别的路可走
            text = CANNOT_READ_CARD
        else:
            text = (
                "你回复的那条里没有可提取的文字——图片、语音、纯图卡片都可能这样，"
                "飞书接口给回来的正文是空的，所以我没敢乱记。\n"
                "把要记的结论直接打给我就行，例如「记一下 xxx 的根因：…」。"
            )
        if context_error:
            text += f"\n（想翻上游消息补齐上下文，也没成：{context_error}）"
        return Outgoing(reply_to=incoming.message_id, text=text)
    slow_query = ""
    try:
        if cmd.kind == "help":
            text = HELP_TEXT
        elif cmd.kind == "status":
            text = _do_status(sb)
        elif cmd.kind == "remember":
            text = _do_remember(sb, cmd)
        else:
            text, hit = _do_ask(sb, cmd.query)
            if not hit and llm_available(sb):
                slow_query = cmd.query
    except Exception as exc:  # noqa: BLE001 - 出错也得回一句，否则对方以为机器人死了
        text = f"处理失败：{exc}"

    return Outgoing(
        reply_to=incoming.message_id, text=_clip_reply(text), slow_query=slow_query
    )
