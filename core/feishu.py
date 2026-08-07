"""飞书/Lark 文档读取（记忆沙箱内置，不依赖 Cursor/Trae MCP）。"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import FeishuConfig

# wiki / docx / docs 链接
_URL_RE = re.compile(
    r"https?://[^\s<>\"']+?(?:larkoffice|feishu|larksuite)\.(?:com|cn)/"
    r"(?P<kind>wiki|docx|docs|doc)/(?P<token>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
@dataclass
class FeishuDocRef:
    url: str
    kind: str  # wiki | docx | docs | doc
    token: str


@dataclass
class FeishuFetchResult:
    url: str
    ok: bool
    title: str = ""
    content: str = ""
    error: str = ""
    document_id: str = ""


@dataclass
class FeishuTitleUpdateResult:
    url: str
    ok: bool
    old_title: str = ""
    new_title: str = ""
    error: str = ""


@dataclass
class FeishuCreateDocResult:
    ok: bool
    document_id: str = ""
    url: str = ""
    title: str = ""
    blocks_written: int = 0
    error: str = ""


@dataclass
class FeishuBodyPreview:
    """改正文前的只读预览，用来确认改的是哪一篇、会动多少内容。"""

    url: str
    ok: bool
    document_id: str = ""
    title: str = ""
    block_count: int = 0
    error: str = ""


@dataclass
class FeishuBodyUpdateResult:
    url: str
    ok: bool
    document_id: str = ""
    title: str = ""
    mode: str = ""
    blocks_written: int = 0
    blocks_deleted: int = 0
    error: str = ""


@dataclass
class FeishuBlockUpdateResult:
    """改单个块的结果。评论机器人只允许这种最小改动，不做全文重写。"""

    url: str
    ok: bool
    document_id: str = ""
    title: str = ""
    block_id: str = ""
    old_text: str = ""
    new_text: str = ""
    error: str = ""


@dataclass
class FeishuCommentReply:
    """评论串里的一条回复。评论机器人要按 reply_id 找「刚发的那条」，光有正文不够。"""

    reply_id: str = ""
    user_id: str = ""
    created_at: str = ""
    text: str = ""


@dataclass
class FeishuComment:
    comment_id: str = ""
    user_id: str = ""
    created_at: str = ""
    is_whole: bool = True
    is_solved: bool = False
    # 局部评论选中的原文；API 加不了局部评论，但客户端里加的能读到
    quote: str = ""
    replies: List[str] = field(default_factory=list)
    reply_items: List[FeishuCommentReply] = field(default_factory=list)


@dataclass
class FeishuCommentListResult:
    url: str
    ok: bool
    document_id: str = ""
    title: str = ""
    comments: List[FeishuComment] = field(default_factory=list)
    truncated: bool = False
    error: str = ""


@dataclass
class FeishuCommentResult:
    url: str
    ok: bool
    document_id: str = ""
    title: str = ""
    comment_id: str = ""
    replied_to: str = ""
    # 回复已有评论时才有：新回复在那条评论串里的 id（贴表情要用它）
    reply_id: str = ""
    # 非空表示这是锚定到该块的局部评论（划词评论）
    block_id: str = ""
    error: str = ""


def extract_feishu_urls(text: str) -> List[FeishuDocRef]:
    """从用户输入提取飞书文档链接（去重保序）。"""
    seen = set()
    out: List[FeishuDocRef] = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(")。.,，、；;")
        kind = m.group("kind").lower()
        token = m.group("token")
        key = (kind, token)
        if key in seen:
            continue
        seen.add(key)
        out.append(FeishuDocRef(url=url, kind=kind, token=token))
    return out


def extract_feishu_tokens(text: str) -> Set[str]:
    """提取文本中的飞书文档 token（来自完整 URL）。"""
    return {ref.token for ref in extract_feishu_urls(text or "")}


def record_matches_feishu_tokens(
    record_texts: Iterable[str],
    required_tokens: Set[str],
) -> bool:
    """
    查询带飞书 token 时：记忆正文须包含至少一个相同 token，
    避免「客服文档」类相似问法串到另一篇 wiki。
    """
    if not required_tokens:
        return True
    blob = "\n".join(t for t in record_texts if t)
    if not blob:
        return False
    return any(token in blob for token in required_tokens)


def feishu_configured(cfg: FeishuConfig) -> bool:
    if not cfg or not cfg.enabled:
        return False
    app_id = (cfg.app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (cfg.app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
    # 至少需要应用凭证；有 user token 时可读个人可见文档
    return bool(app_id) and bool(app_secret)


def _resolve_credentials(cfg: FeishuConfig) -> Tuple[str, str, str, str]:
    app_id = (cfg.app_id or os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (cfg.app_secret or os.environ.get("FEISHU_APP_SECRET") or "").strip()
    user_token = (
        cfg.user_access_token or os.environ.get("FEISHU_USER_ACCESS_TOKEN") or ""
    ).strip()
    api_base = (cfg.api_base or os.environ.get("FEISHU_API_BASE") or "https://open.feishu.cn").rstrip(
        "/"
    )
    return app_id, app_secret, user_token, api_base


def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> dict:
    data = None
    req_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}") from e
    if not raw:
        return {}
    return json.loads(raw)


def _tenant_access_token(api_base: str, app_id: str, app_secret: str, timeout: float) -> str:
    """获取 tenant_access_token（部分接口可用；读个人文档仍优先 user token）。"""
    url = f"{api_base}/open-apis/auth/v3/tenant_access_token/internal"
    data = _http_json(
        "POST",
        url,
        body={"app_id": app_id, "app_secret": app_secret},
        timeout=timeout,
    )
    if data.get("code") not in (0, None) and "tenant_access_token" not in data:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg') or data}")
    token = data.get("tenant_access_token") or ""
    if not token:
        raise RuntimeError(f"tenant_access_token 为空: {data}")
    return token


def _wiki_node_info(
    api_base: str, access_token: str, wiki_token: str, timeout: float
) -> dict:
    """wiki 节点原始信息；改标题需要其中的 space_id / node_token。"""
    q = urllib.parse.urlencode({"token": wiki_token, "obj_type": "wiki"})
    url = f"{api_base}/open-apis/wiki/v2/spaces/get_node?{q}"
    data = _http_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"wiki get_node 失败: {data.get('msg') or data}")
    node = (data.get("data") or {}).get("node") or {}
    if not node.get("obj_token"):
        raise RuntimeError("未能获取文档 obj_token（可能无权限或 token 无效）")
    return node


def _wiki_node(
    api_base: str, access_token: str, wiki_token: str, timeout: float
) -> Tuple[str, str]:
    """返回 (obj_token, title)。"""
    node = _wiki_node_info(api_base, access_token, wiki_token, timeout)
    return node.get("obj_token") or "", str(node.get("title") or "").strip()


def _wiki_obj_token(api_base: str, access_token: str, wiki_token: str, timeout: float) -> str:
    obj, _title = _wiki_node(api_base, access_token, wiki_token, timeout)
    return obj


def _docx_title(api_base: str, access_token: str, document_id: str, timeout: float) -> str:
    path = urllib.parse.quote(document_id, safe="")
    url = f"{api_base}/open-apis/docx/v1/documents/{path}"
    data = _http_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if data.get("code") != 0:
        return ""
    doc = (data.get("data") or {}).get("document") or {}
    return str(doc.get("title") or "").strip()


def _docx_raw_content(api_base: str, access_token: str, document_id: str, timeout: float) -> str:
    path = urllib.parse.quote(document_id, safe="")
    url = f"{api_base}/open-apis/docx/v1/documents/{path}/raw_content"
    data = _http_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"docx raw_content 失败: {data.get('msg') or data}")
    content = ((data.get("data") or {}).get("content")) or ""
    return str(content)


def _auth_tokens_to_try(
    api_base: str, app_id: str, app_secret: str, user_token: str, timeout: float
) -> List[Tuple[str, str]]:
    """优先 user token（个人可见文档），失败再试 tenant token（应用已授权文档）。"""
    ordered: List[Tuple[str, str]] = []
    if user_token:
        ordered.append(("user_access_token", user_token))
    tenant = _tenant_access_token(api_base, app_id, app_secret, timeout)
    ordered.append(("tenant_access_token", tenant))
    return ordered


def _is_user_token_error(err: str) -> bool:
    """user_access_token 失效/过期，值得用 refresh_token 强刷一次再读。"""
    # 99991668 无效 token；99991677 token 过期；两者都只有强刷能救
    codes = ("99991668", "99991677")
    texts = ("invalid access token", "token expired", "access token expired")
    low = err.lower()
    return any(c in err for c in codes) or any(t in low for t in texts)


def _widget_tail(
    api_base: str, access_token: str, document_id: str, timeout: float
) -> str:
    """组件附录，拼在正文后面。绝不抛异常：读不到组件不该让整篇正文读失败。"""
    from .feishu_widgets import widget_appendix

    try:
        blocks = _all_blocks(api_base, access_token, document_id, timeout)
        tail = widget_appendix(api_base, access_token, blocks, timeout)
    except Exception as e:  # noqa: BLE001
        tail = f"【文档组件附录】列块失败，未能检查画板等组件：{e}"
    return f"\n\n{tail}" if tail else ""


def fetch_feishu_document(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    *,
    config_path: Optional[str] = None,
    include_widgets: bool = False,
) -> FeishuFetchResult:
    """
    拉取单篇飞书文档纯文本。

    include_widgets=True 时额外列一遍文档块，把画板这类「正文里没有痕迹」的
    组件读出来附在末尾。默认关掉：多两次请求，而大多数文档没有组件。
    """
    if not feishu_configured(cfg):
        return FeishuFetchResult(
            url=ref.url,
            ok=False,
            error=(
                "未配置飞书：请在 Application Support/MemorySandbox/config.yaml 设置 "
                "feishu.app_id / app_secret，然后运行 python3 scripts/feishu_login.py "
                "浏览器授权获取 user_access_token（管理后台看不到明文）"
            ),
        )

    # 过期则用 refresh_token 动态续期；失败原因留到最终错误里，便于定位
    # （tenant token 仍可能读成功，所以这里不直接返回失败）
    prep_errors: List[str] = []
    try:
        from .feishu_oauth import ensure_user_access_token

        ensure_user_access_token(cfg, config_path=config_path)
    except Exception as e:
        prep_errors.append(f"续期: {e}")

    app_id, app_secret, user_token, api_base = _resolve_credentials(cfg)
    timeout = float(cfg.timeout or 30)
    try:
        tokens = _auth_tokens_to_try(api_base, app_id, app_secret, user_token, timeout)
    except Exception as e:
        return FeishuFetchResult(url=ref.url, ok=False, error=f"获取应用凭证失败: {e}")

    def _read_with(access_token: str) -> Tuple[str, str, str]:
        """返回 (document_id, content, title)。"""
        document_id = ref.token
        title = ""
        if ref.kind == "wiki":
            document_id, title = _wiki_node(api_base, access_token, ref.token, timeout)
        content = _docx_raw_content(api_base, access_token, document_id, timeout)
        if not title:
            title = _docx_title(api_base, access_token, document_id, timeout)
        if include_widgets:
            content = content.rstrip() + _widget_tail(
                api_base, access_token, document_id, timeout
            )
        return document_id, content, title

    errors: List[str] = list(prep_errors)
    refreshed_once = False
    for label, access_token in tokens:
        try:
            document_id, content, title = _read_with(access_token)
            if not content.strip():
                errors.append(f"{label}: 正文为空")
                continue
            max_chars = int(cfg.max_chars or 80000)
            trimmed = content
            if max_chars > 0 and len(trimmed) > max_chars:
                trimmed = trimmed[:max_chars] + f"\n\n…(已截断，原文 {len(content)} 字)"
            return FeishuFetchResult(
                url=ref.url,
                ok=True,
                title=title or ref.token,
                content=trimmed,
                document_id=document_id,
            )
        except Exception as e:
            err = str(e)
            errors.append(f"{label}: {err}")
            # user token 失效：强制 refresh 再读一次
            if (
                label == "user_access_token"
                and not refreshed_once
                and _is_user_token_error(err)
            ):
                try:
                    from .feishu_oauth import ensure_user_access_token

                    new_tok = ensure_user_access_token(
                        cfg, config_path=config_path, force_refresh=True
                    )
                    refreshed_once = True
                    document_id, content, title = _read_with(new_tok)
                    if content.strip():
                        max_chars = int(cfg.max_chars or 80000)
                        trimmed = content
                        if max_chars > 0 and len(trimmed) > max_chars:
                            trimmed = (
                                trimmed[:max_chars]
                                + f"\n\n…(已截断，原文 {len(content)} 字)"
                            )
                        return FeishuFetchResult(
                            url=ref.url,
                            ok=True,
                            title=title or ref.token,
                            content=trimmed,
                            document_id=document_id,
                        )
                    errors.append("user_access_token(refreshed): 正文为空")
                except Exception as re:
                    errors.append(f"refresh: {re}")

    hint = (
        "；个人文档请运行 python3 scripts/feishu_login.py 重新授权；"
        "应用可读文档需在开放平台开通 wiki/docx 读权限并添加文档授权"
    )
    return FeishuFetchResult(
        url=ref.url,
        ok=False,
        error="；".join(errors) + hint,
    )


def fetch_feishu_docs_for_text(
    cfg: FeishuConfig,
    text: str,
    *,
    config_path: Optional[str] = None,
) -> Tuple[List[FeishuFetchResult], str]:
    """
    识别输入中的飞书链接并拉取正文。
    返回 (结果列表, 拼好的上下文字符串)。
    """
    refs = extract_feishu_urls(text)
    if not refs:
        return [], ""
    results: List[FeishuFetchResult] = []
    blocks: List[str] = []
    for ref in refs:
        r = fetch_feishu_document(cfg, ref, config_path=config_path)
        results.append(r)
        if r.ok:
            title = (r.title or "").strip()
            title_line = f"### 飞书文档：{title}\n" if title else "### 飞书文档\n"
            blocks.append(
                f"{title_line}"
                f"{r.url}\n"
                f"(document_id={r.document_id})\n\n{r.content}"
            )
        else:
            blocks.append(f"### 飞书文档 {r.url}\n读取失败：{r.error}")
    return results, "\n\n".join(blocks)


def _post_wiki_title(
    api_base: str,
    access_token: str,
    space_id: str,
    node_token: str,
    title: str,
    timeout: float,
) -> None:
    space = urllib.parse.quote(str(space_id), safe="")
    node = urllib.parse.quote(str(node_token), safe="")
    url = f"{api_base}/open-apis/wiki/v2/spaces/{space}/nodes/{node}/update_title"
    data = _http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        body={"title": title},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"update_title 失败: {data.get('msg') or data}")


def update_wiki_node_title(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    new_title: str,
    *,
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> FeishuTitleUpdateResult:
    """
    改飞书 wiki 节点标题（POST wiki/v2/spaces/:space_id/nodes/:node_token/update_title）。

    只走 user_access_token：写操作用 tenant token 会以应用身份改动，且要求应用
    是知识库成员，行为不如「以本人身份改」可预期。

    confirmed 必须显式传 True。改动的是团队共享文档，约定为每次都要由本人确认；
    默认拒绝可以保证没有任何调用方（CLI / MCP / 脚本）能静默改掉别人的文档。
    """
    if not confirmed:
        return FeishuTitleUpdateResult(
            url=ref.url,
            ok=False,
            error="未确认：改飞书文档需本人逐次确认，调用方须显式传 confirmed=True",
        )
    title = (new_title or "").strip()
    if not title:
        return FeishuTitleUpdateResult(url=ref.url, ok=False, error="新标题不能为空")
    if len(title) > 800:
        return FeishuTitleUpdateResult(
            url=ref.url, ok=False, error=f"新标题过长（{len(title)} 字）"
        )
    if ref.kind != "wiki":
        return FeishuTitleUpdateResult(
            url=ref.url,
            ok=False,
            error=(
                f"只支持 wiki 节点改标题，当前链接是 {ref.kind}。"
                "docx 直链没有 space_id/node_token，需用该文档在知识库中的 wiki 链接"
            ),
        )
    if not feishu_configured(cfg):
        return FeishuTitleUpdateResult(
            url=ref.url, ok=False, error="未配置飞书 app_id / app_secret"
        )

    from .feishu_oauth import ensure_user_access_token

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)
    try:
        token = ensure_user_access_token(cfg, config_path=config_path)
    except Exception as e:
        return FeishuTitleUpdateResult(
            url=ref.url,
            ok=False,
            error=f"获取 user_access_token 失败: {e}；请运行 python3 scripts/feishu_login.py",
        )

    def _rename(access_token: str) -> Tuple[str, str]:
        node = _wiki_node_info(api_base, access_token, ref.token, timeout)
        space_id = str(node.get("space_id") or "")
        node_token = str(node.get("node_token") or ref.token)
        if not space_id:
            raise RuntimeError("节点信息里没有 space_id，无法改标题")
        old = str(node.get("title") or "").strip()
        _post_wiki_title(api_base, access_token, space_id, node_token, title, timeout)
        return old, node_token

    try:
        old_title, _ = _rename(token)
    except Exception as e:
        err = str(e)
        if not _is_user_token_error(err):
            hint = ""
            if "131006" in err or "permission denied" in err.lower():
                hint = (
                    "；改标题属写操作，需要该节点的容器编辑权限，"
                    "并确认应用已开通 wiki:node:update"
                )
            return FeishuTitleUpdateResult(url=ref.url, ok=False, error=err + hint)
        # token 失效：强刷一次再试
        try:
            fresh = ensure_user_access_token(
                cfg, config_path=config_path, force_refresh=True
            )
            old_title, _ = _rename(fresh)
        except Exception as e2:
            return FeishuTitleUpdateResult(
                url=ref.url, ok=False, error=f"{err}；重试后仍失败: {e2}"
            )

    return FeishuTitleUpdateResult(
        url=ref.url, ok=True, old_title=old_title, new_title=title
    )


# docx block_type 与 BlockData 字段名必须对应，否则接口报 schema mismatch(1770006)
_BLOCK_TEXT = 2
_BLOCK_BULLET = 12
_BLOCK_ORDERED = 13
_BLOCK_CODE = 14
_BLOCK_QUOTE = 15
_BLOCK_DIVIDER = 22
_BLOCK_TABLE = 31
_BLOCK_TABLE_CELL = 32
# 单元格文本暂存在这个私有键上，写入时展开成嵌套块，不会发给接口
_TABLE_CELLS = "_ms_table_cells"
# 列宽分配。飞书正文区宽度约 800px；接口下限是 50px，但 50px 放不下几个字，
# 所以自己按 100px 保底。MAX 只封顶「需求量」，避免一段长文本把权重拉到别的列几乎为零；
# 按预算缩放后单列实际宽度仍可能超过它
_TABLE_TOTAL_WIDTH = 800
_TABLE_MIN_WIDTH = 100
_TABLE_MAX_WIDTH = 420
_TABLE_PX_PER_UNIT = 8
_TABLE_CELL_PADDING = 24
# Markdown 一级标题对应 heading1(3)，逐级递增到 heading6(8)
_HEADING_BLOCK = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8}
_BLOCK_FIELD: Dict[int, str] = {
    _BLOCK_TEXT: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    6: "heading4",
    7: "heading5",
    8: "heading6",
    _BLOCK_BULLET: "bullet",
    _BLOCK_ORDERED: "ordered",
    _BLOCK_CODE: "code",
    _BLOCK_QUOTE: "quote",
}
# 创建块接口单次 children 长度上限 50，且单应用每秒 3 次
_BLOCK_BATCH = 50
_BLOCK_WRITE_INTERVAL = 0.4

_FENCE_RE = re.compile(r"^\s*```")
_DIVIDER_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")

# 表格：以 | 分隔的行，且第二行是 |---|:--:| 这样的分隔行才认作表格，
# 免得正文里偶然出现一个竖线就被当成表
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_TABLE_SEP_RE = re.compile(r"^\|(?:\s*:?-{1,}:?\s*\|)+$")

# 行内语法。优先级：行内代码 > 链接 > 粗体 > 删除线 > 斜体
# （代码里的 * 不该再当粗体解析）
_INLINE_PATTERNS = (
    ("code", re.compile(r"`([^`\n]+)`")),
    ("link", re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)\)")),
    ("bold", re.compile(r"\*\*(\S(?:.*?\S)?)\*\*|__(\S(?:.*?\S)?)__")),
    ("strike", re.compile(r"~~(\S(?:.*?\S)?)~~")),
    # 分隔符内侧不能是空白，否则「2 * 3 * 4」会被当成斜体
    (
        "italic",
        re.compile(
            r"(?<![*\w])\*(\S(?:[^*\n]*\S)?)\*(?![*\w])"
            r"|(?<![_\w])_(\S(?:[^_\n]*\S)?)_(?![_\w])"
        ),
    ),
)
_STYLE_KEY = {
    "bold": "bold",
    "italic": "italic",
    "strike": "strikethrough",
    "code": "inline_code",
}


def _element(content: str, style: Optional[dict] = None) -> dict:
    run: Dict[str, object] = {"content": content}
    if style:
        run["text_element_style"] = style
    return {"text_run": run}


def _inline_elements(text: str, base: Optional[dict] = None) -> List[dict]:
    """
    把行内 Markdown 拆成带样式的 text_run 列表。

    不解析的话 **粗体** 会原样写成字面量，表格里尤其明显。
    """
    src = text or ""
    if not src:
        return [_element("", base)]
    out: List[dict] = []
    pos = 0
    while pos < len(src):
        best: Optional[Tuple[int, str, "re.Match[str]"]] = None
        for kind, pat in _INLINE_PATTERNS:
            m = pat.search(src, pos)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), kind, m)
        if best is None:
            out.append(_element(src[pos:], base))
            break
        start, kind, m = best
        if start > pos:
            out.append(_element(src[pos:start], base))
        style = dict(base or {})
        if kind == "link":
            inner = m.group(1)
            # 飞书按 element 挂链接；URL 要转义，否则中文/特殊字符会丢
            style["link"] = {"url": urllib.parse.quote(m.group(2), safe="")}
            out.extend(_inline_elements(inner, style) if inner else [_element(m.group(2), style)])
        elif kind == "code":
            # 代码内容不再往下解析
            style[_STYLE_KEY[kind]] = True
            out.append(_element(m.group(1), style))
        else:
            style[_STYLE_KEY[kind]] = True
            inner = next(g for g in m.groups() if g is not None)
            out.extend(_inline_elements(inner, style))
        pos = m.end()
    return [e for e in out if e["text_run"]["content"] != ""] or [_element("", base)]


def _text_block(block_type: int, content: str, *, parse_inline: bool = True) -> dict:
    elements = (
        _inline_elements(content)
        if parse_inline
        else [_element(content)]
    )
    return {
        "block_type": block_type,
        _BLOCK_FIELD[block_type]: {"elements": elements, "style": {}},
    }


def _split_table_row(line: str) -> List[str]:
    """拆一行表格；\\| 是转义的竖线，不当分隔符。"""
    body = line.strip().strip("|")
    cells: List[str] = []
    buf: List[str] = []
    escaped = False
    for ch in body:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    cells.append("".join(buf).strip())
    return cells


def _display_units(text: str) -> int:
    """按显示宽度计长度：中日韩文字与全角标点占两格。"""
    units = 0
    for ch in text:
        units += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return units


def _cell_plain_text(cell: str) -> str:
    """单元格渲染后的纯文本：`code`、**粗体**、[文字](链接) 的标记不占宽度。"""
    return "".join(
        str(e["text_run"]["content"]) for e in _inline_elements(cell)
    )


def _column_widths(grid: List[List[str]]) -> List[int]:
    """
    按每列最长内容分配列宽。

    不给 column_width 的话飞书会用一个偏小的固定默认值平分，像
    `modules/account/src/pages/Settlement/index.tsx` 这种长路径会被挤成一列一个字。
    """
    cols = len(grid[0]) if grid else 0
    if cols <= 0:
        return []

    demand: List[int] = []
    for c in range(cols):
        units = max(_display_units(_cell_plain_text(row[c])) for row in grid)
        raw = units * _TABLE_PX_PER_UNIT + _TABLE_CELL_PADDING
        demand.append(min(max(raw, _TABLE_MIN_WIDTH), _TABLE_MAX_WIDTH))

    # 列太多时正文宽度肯定放不下，宁可让飞书横向滚动，也不要把每列压到看不清
    budget = max(_TABLE_TOTAL_WIDTH, cols * _TABLE_MIN_WIDTH)
    total = sum(demand)
    widths = [max(_TABLE_MIN_WIDTH, round(d * budget / total)) for d in demand]

    # 保底可能把总宽顶超预算，从最宽的列里一点点扣回来
    while sum(widths) > budget:
        widest = max(widths)
        if widest <= _TABLE_MIN_WIDTH:
            break
        widths[widths.index(widest)] -= 1
    drift = budget - sum(widths)
    if drift > 0:
        widths[widths.index(max(widths))] += drift
    return widths


def _table_block(rows: List[List[str]]) -> dict:
    """
    表格块。真正的嵌套结构在写入时用「创建嵌套块」接口展开，
    这里先把单元格文本挂在私有字段上。
    """
    cols = max(len(r) for r in rows)
    grid = [r + [""] * (cols - len(r)) for r in rows]
    return {
        "block_type": _BLOCK_TABLE,
        "table": {
            "property": {
                "row_size": len(grid),
                "column_size": cols,
                "column_width": _column_widths(grid),
                # Markdown 表格第一行就是表头
                "header_row": True,
            }
        },
        _TABLE_CELLS: grid,
    }


def markdown_to_docx_blocks(text: str) -> List[dict]:
    """
    把 Markdown 子集转成 docx block 列表。

    支持 #~###### 标题、- / * 无序列表、1. 有序列表、``` 代码块、> 引用、--- 分割线、
    GFM 管道表格，以及行内 **粗体** / *斜体* / ~~删除线~~ / `代码` / [文字](链接)。
    代码块内不解析行内语法。
    """
    lines = (text or "").splitlines()
    blocks: List[dict] = []
    code_lines: List[str] = []
    in_code = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        if _FENCE_RE.match(raw):
            if in_code:
                blocks.append(
                    _text_block(_BLOCK_CODE, "\n".join(code_lines), parse_inline=False)
                )
                code_lines = []
            in_code = not in_code
            i += 1
            continue
        if in_code:
            code_lines.append(raw)
            i += 1
            continue
        line = raw.strip()
        if not line:
            i += 1
            continue
        # 表格要看下一行是不是分隔行，所以先于分割线判断：
        # |---|---| 这种行本身也能匹配 --- 之外的规则
        if (
            _TABLE_ROW_RE.match(line)
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1].strip().replace(" ", ""))
        ):
            rows = [_split_table_row(line)]
            i += 2
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                rows.append(_split_table_row(lines[i].strip()))
                i += 1
            blocks.append(_table_block(rows))
            continue
        if _DIVIDER_RE.match(line):
            blocks.append({"block_type": _BLOCK_DIVIDER, "divider": {}})
            i += 1
            continue
        m = _HEADING_RE.match(line)
        if m:
            blocks.append(
                _text_block(_HEADING_BLOCK[len(m.group(1))], m.group(2).strip())
            )
            i += 1
            continue
        m = _BULLET_RE.match(line)
        if m:
            blocks.append(_text_block(_BLOCK_BULLET, m.group(1).strip()))
            i += 1
            continue
        m = _ORDERED_RE.match(line)
        if m:
            blocks.append(_text_block(_BLOCK_ORDERED, m.group(1).strip()))
            i += 1
            continue
        m = _QUOTE_RE.match(line)
        if m:
            blocks.append(_text_block(_BLOCK_QUOTE, m.group(1).strip()))
            i += 1
            continue
        blocks.append(_text_block(_BLOCK_TEXT, line))
        i += 1
    # 围栏没闭合也不要丢内容
    if in_code and code_lines:
        blocks.append(_text_block(_BLOCK_CODE, "\n".join(code_lines), parse_inline=False))
    return blocks


def _create_docx(
    api_base: str,
    access_token: str,
    title: str,
    folder_token: str,
    timeout: float,
) -> Tuple[str, str]:
    """建一篇空 docx，返回 (document_id, title)。"""
    body: Dict[str, str] = {}
    if title:
        body["title"] = title
    if folder_token:
        body["folder_token"] = folder_token
    data = _http_json(
        "POST",
        f"{api_base}/open-apis/docx/v1/documents",
        headers={"Authorization": f"Bearer {access_token}"},
        body=body,
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"创建文档失败: {data.get('msg') or data}")
    doc = (data.get("data") or {}).get("document") or {}
    document_id = str(doc.get("document_id") or "")
    if not document_id:
        raise RuntimeError(f"创建文档成功但没返回 document_id: {data}")
    return document_id, str(doc.get("title") or "")


def _table_descendants(table: dict, seq: int) -> dict:
    """
    把表格块展开成「创建嵌套块」接口的载荷。

    表格是 table → table_cell → 文本 的三层结构，平铺的 children 接口建不出来，
    只能走 descendant；descendants 里是所有块的**平铺**列表，靠临时 block_id 关联。
    空单元格也必须挂一个文本子块，否则接口报错。
    """
    grid: List[List[str]] = table.get(_TABLE_CELLS) or [[]]
    table_id = f"t{seq}"
    cell_ids: List[str] = []
    descendants: List[dict] = []
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            cell_id = f"t{seq}c{r}_{c}"
            text_id = f"{cell_id}t"
            cell_ids.append(cell_id)
            descendants.append(
                {
                    "block_id": cell_id,
                    "block_type": _BLOCK_TABLE_CELL,
                    "table_cell": {},
                    "children": [text_id],
                }
            )
            descendants.append(
                {
                    "block_id": text_id,
                    "block_type": _BLOCK_TEXT,
                    "text": {"elements": _inline_elements(cell), "style": {}},
                    "children": [],
                }
            )
    table_block = {
        "block_id": table_id,
        "block_type": _BLOCK_TABLE,
        "table": table.get("table") or {},
        "children": cell_ids,
    }
    return {
        "index": -1,
        "children_id": [table_id],
        "descendants": [table_block] + descendants,
    }


def _append_docx_blocks(
    api_base: str,
    access_token: str,
    document_id: str,
    blocks: List[dict],
    timeout: float,
) -> int:
    """
    把 block 追加到文档根节点，返回已写入数量。

    平铺块走 children 接口分批写；表格必须单独走 descendant 接口。两者按原顺序
    交替发送，且都是「追加到末尾」，所以混排的顺序不会乱。
    """
    # 根节点的 block_id 就是 document_id
    path = urllib.parse.quote(document_id, safe="")
    children_url = f"{api_base}/open-apis/docx/v1/documents/{path}/blocks/{path}/children"
    descendant_url = (
        f"{api_base}/open-apis/docx/v1/documents/{path}/blocks/{path}/descendant"
        "?document_revision_id=-1"
    )
    written = 0
    sent = 0

    def _post(url: str, body: dict) -> None:
        nonlocal sent
        if sent:
            time.sleep(_BLOCK_WRITE_INTERVAL)
        data = _http_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            body=body,
            timeout=timeout,
        )
        sent += 1
        if data.get("code") != 0:
            raise RuntimeError(
                f"写入正文失败（已写 {written} 块）: {data.get('msg') or data}"
            )

    flat: List[dict] = []

    def _flush_flat() -> None:
        nonlocal written, flat
        for start in range(0, len(flat), _BLOCK_BATCH):
            batch = flat[start : start + _BLOCK_BATCH]
            _post(children_url, {"index": -1, "children": batch})
            written += len(batch)
        flat = []

    for idx, block in enumerate(blocks):
        if block.get("block_type") == _BLOCK_TABLE:
            # 先把攒着的平铺块写掉，保证表格落在正确位置
            _flush_flat()
            _post(descendant_url, _table_descendants(block, idx))
            written += 1
            continue
        flat.append(block)
        if len(flat) >= _BLOCK_BATCH:
            _flush_flat()
    _flush_flat()
    return written


def _docx_url(cfg: FeishuConfig, document_id: str) -> str:
    host = (getattr(cfg, "doc_host", "") or "").strip().rstrip("/")
    if not host:
        return ""
    host = re.sub(r"^https?://", "", host)
    return f"https://{host}/docx/{document_id}"


def create_docx_document(
    cfg: FeishuConfig,
    title: str,
    *,
    content: str = "",
    folder_token: str = "",
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> FeishuCreateDocResult:
    """
    在云空间新建 docx 文档，可选把 Markdown 正文写进去。

    只走 user_access_token：以本人身份创建，文档才落在本人云空间且本人可见；
    tenant token 会把文档创建在应用名下。

    confirmed 必须显式传 True，与改标题同一门禁：新建文档同样是飞书侧的写操作，
    约定为每次都由本人确认，默认拒绝可保证没有调用方能静默建文档。
    """
    if not confirmed:
        return FeishuCreateDocResult(
            ok=False,
            error="未确认：新建飞书文档需本人逐次确认，调用方须显式传 confirmed=True",
        )
    doc_title = (title or "").strip()
    if not doc_title:
        return FeishuCreateDocResult(ok=False, error="标题不能为空")
    if len(doc_title) > 800:
        return FeishuCreateDocResult(
            ok=False, error=f"标题过长（{len(doc_title)} 字，上限 800）"
        )
    if not feishu_configured(cfg):
        return FeishuCreateDocResult(ok=False, error="未配置飞书 app_id / app_secret")

    from .feishu_oauth import ensure_user_access_token

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)
    try:
        token = ensure_user_access_token(cfg, config_path=config_path)
    except Exception as e:
        return FeishuCreateDocResult(
            ok=False,
            error=f"获取 user_access_token 失败: {e}；请运行 python3 scripts/feishu_login.py",
        )

    blocks = markdown_to_docx_blocks(content)

    def _permission_hint(err: str) -> str:
        low = err.lower()
        if "1770040" in err or "1770032" in err or "permission" in low:
            return (
                "；创建需 docx:document:create、写正文需 docx:document:write_only"
                "（后台没有 docx:document 这一项，只能勾细分权限），"
                "指定文件夹时还需该文件夹的编辑权限"
            )
        return ""

    try:
        document_id, created_title = _create_docx(
            api_base, token, doc_title, folder_token.strip(), timeout
        )
    except Exception as e:
        err = str(e)
        if not _is_user_token_error(err):
            return FeishuCreateDocResult(ok=False, error=err + _permission_hint(err))
        try:
            token = ensure_user_access_token(
                cfg, config_path=config_path, force_refresh=True
            )
            document_id, created_title = _create_docx(
                api_base, token, doc_title, folder_token.strip(), timeout
            )
        except Exception as e2:
            return FeishuCreateDocResult(ok=False, error=f"{err}；重试后仍失败: {e2}")

    url = _docx_url(cfg, document_id)
    written = 0
    if blocks:
        try:
            written = _append_docx_blocks(api_base, token, document_id, blocks, timeout)
        except Exception as e:
            err = str(e)
            # 文档已经建出来了，必须把 id 带回去，否则用户不知道有半成品要清理
            return FeishuCreateDocResult(
                ok=False,
                document_id=document_id,
                url=url,
                title=created_title or doc_title,
                blocks_written=0,
                error=f"文档已创建但正文写入失败：{err}{_permission_hint(err)}",
            )

    return FeishuCreateDocResult(
        ok=True,
        document_id=document_id,
        url=url,
        title=created_title or doc_title,
        blocks_written=written,
    )


def _root_children(
    api_base: str, access_token: str, document_id: str, timeout: float
) -> List[dict]:
    """列出文档根节点的直接子块（分页取完）。删除要按子块索引，所以得先数清楚。"""
    path = urllib.parse.quote(document_id, safe="")
    base = f"{api_base}/open-apis/docx/v1/documents/{path}/blocks/{path}/children"
    items: List[dict] = []
    page_token = ""
    while True:
        query = {"page_size": 500}
        if page_token:
            query["page_token"] = page_token
        data = _http_json(
            "GET",
            f"{base}?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"获取子块失败: {data.get('msg') or data}")
        payload = data.get("data") or {}
        items.extend(payload.get("items") or [])
        page_token = str(payload.get("page_token") or "")
        if not payload.get("has_more") or not page_token:
            return items


def _delete_root_children(
    api_base: str,
    access_token: str,
    document_id: str,
    count: int,
    timeout: float,
) -> int:
    """清掉根节点前 count 个子块，返回删除数量。"""
    path = urllib.parse.quote(document_id, safe="")
    url = (
        f"{api_base}/open-apis/docx/v1/documents/{path}"
        f"/blocks/{path}/children/batch_delete"
    )
    deleted = 0
    remaining = count
    while remaining > 0:
        # 区间左闭右开；每轮都删最前面一批，删完后面的块会自动前移
        chunk = min(remaining, _BLOCK_BATCH)
        if deleted:
            time.sleep(_BLOCK_WRITE_INTERVAL)
        data = _http_json(
            "DELETE",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            body={"start_index": 0, "end_index": chunk},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"删除原正文失败（已删 {deleted} 块）: {data.get('msg') or data}"
            )
        deleted += chunk
        remaining -= chunk
    return deleted


def _resolve_document_id(
    api_base: str, access_token: str, ref: FeishuDocRef, timeout: float
) -> Tuple[str, str]:
    """返回 (document_id, title)。wiki 链接要先换成 docx 的 obj_token。"""
    if ref.kind == "wiki":
        return _wiki_node(api_base, access_token, ref.token, timeout)
    return ref.token, _docx_title(api_base, access_token, ref.token, timeout)


def _with_user_token(
    cfg: FeishuConfig,
    config_path: Optional[str],
    read_step,
):
    """
    跑一次只读步骤；user token 失效则强刷再跑一次。
    返回 (token, 结果)：后续写操作复用这里已验证过的 token，避免写到一半才发现过期。
    """
    from .feishu_oauth import ensure_user_access_token

    token = ensure_user_access_token(cfg, config_path=config_path)
    try:
        return token, read_step(token)
    except Exception as e:
        if not _is_user_token_error(str(e)):
            raise
        token = ensure_user_access_token(
            cfg, config_path=config_path, force_refresh=True
        )
        return token, read_step(token)


def preview_docx_body(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    *,
    config_path: Optional[str] = None,
) -> FeishuBodyPreview:
    """只读：确认改正文的目标文档与现有块数。不受写门禁限制。"""
    if not feishu_configured(cfg):
        return FeishuBodyPreview(
            url=ref.url, ok=False, error="未配置飞书 app_id / app_secret"
        )
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> Tuple[str, str, int]:
        document_id, title = _resolve_document_id(api_base, token, ref, timeout)
        blocks = _root_children(api_base, token, document_id, timeout)
        return document_id, title, len(blocks)

    try:
        _tok, (document_id, title, count) = _with_user_token(cfg, config_path, _read)
    except Exception as e:
        return FeishuBodyPreview(url=ref.url, ok=False, error=str(e))
    return FeishuBodyPreview(
        url=ref.url,
        ok=True,
        document_id=document_id,
        title=title,
        block_count=count,
    )


def update_docx_body(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    content: str,
    *,
    mode: str = "append",
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> FeishuBodyUpdateResult:
    """
    改飞书文档正文。mode=append 追加到末尾；mode=replace 先删原正文再写入。

    只走 user_access_token，与其它写操作同一门禁：confirmed 必须显式传 True，
    否则直接返回错误且不发任何请求。

    replace 会真的删掉原有块，调用方务必先用 preview_docx_body 让本人看清目标文档。
    """
    if not confirmed:
        return FeishuBodyUpdateResult(
            url=ref.url,
            ok=False,
            error="未确认：改飞书文档正文需本人逐次确认，调用方须显式传 confirmed=True",
        )
    if mode not in {"append", "replace"}:
        return FeishuBodyUpdateResult(
            url=ref.url, ok=False, error=f"未知模式 {mode}（只支持 append / replace）"
        )
    blocks = markdown_to_docx_blocks(content)
    if not blocks:
        # replace 传空会把文档清空，这种破坏性操作不该由「正文恰好为空」触发
        return FeishuBodyUpdateResult(
            url=ref.url, ok=False, error="正文为空，不做改动"
        )
    if not feishu_configured(cfg):
        return FeishuBodyUpdateResult(
            url=ref.url, ok=False, error="未配置飞书 app_id / app_secret"
        )

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> Tuple[str, int, str]:
        document_id, title = _resolve_document_id(api_base, token, ref, timeout)
        existing = 0
        if mode == "replace":
            existing = len(_root_children(api_base, token, document_id, timeout))
        return document_id, existing, title

    # 先用只读步骤定位文档并验证 token，写操作再用同一个 token，
    # 避免删到一半才发现过期、重试又重复删
    try:
        token, (document_id, existing, doc_title) = _with_user_token(
            cfg, config_path, _read
        )
    except Exception as e:
        err = str(e)
        hint = ""
        if "1770032" in err or "131006" in err or "permission" in err.lower():
            hint = (
                "；改正文需要该文档的编辑权限，并确认应用已开通 "
                "docx:document:readonly + docx:document:write_only"
            )
        return FeishuBodyUpdateResult(url=ref.url, ok=False, error=err + hint)

    deleted = 0
    if mode == "replace" and existing:
        try:
            deleted = _delete_root_children(
                api_base, token, document_id, existing, timeout
            )
        except Exception as e:
            return FeishuBodyUpdateResult(
                url=ref.url,
                ok=False,
                document_id=document_id,
                title=doc_title,
                mode=mode,
                blocks_deleted=0,
                error=f"{e}；文档可能已被部分清空，可在飞书里用「历史版本」恢复",
            )

    try:
        written = _append_docx_blocks(api_base, token, document_id, blocks, timeout)
    except Exception as e:
        tail = ""
        if deleted:
            tail = f"；原正文 {deleted} 块已删除，可在飞书里用「历史版本」恢复"
        return FeishuBodyUpdateResult(
            url=ref.url,
            ok=False,
            document_id=document_id,
            title=doc_title,
            mode=mode,
            blocks_deleted=deleted,
            error=f"写入正文失败：{e}{tail}",
        )

    return FeishuBodyUpdateResult(
        url=ref.url,
        ok=True,
        document_id=document_id,
        title=doc_title,
        mode=mode,
        blocks_written=written,
        blocks_deleted=deleted,
    )


_BLOCK_META_KEYS = frozenset(
    {"block_id", "block_type", "parent_id", "children", "comment_ids"}
)


def _block_plain_text(block: dict) -> str:
    """取块的纯文本。读接口里文字在 <字段>.elements[].text_run.content。"""
    for key, val in (block or {}).items():
        if key in _BLOCK_META_KEYS or not isinstance(val, dict):
            continue
        elements = val.get("elements")
        if isinstance(elements, list):
            return "".join(
                str((e.get("text_run") or {}).get("content") or "") for e in elements
            )
    return ""


def _all_blocks(
    api_base: str, access_token: str, document_id: str, timeout: float
) -> List[dict]:
    """列出文档所有块（含表格单元格内的文本块），分页取完。"""
    path = urllib.parse.quote(document_id, safe="")
    base = f"{api_base}/open-apis/docx/v1/documents/{path}/blocks"
    items: List[dict] = []
    page_token = ""
    while True:
        query: Dict[str, object] = {"page_size": 500}
        if page_token:
            query["page_token"] = page_token
        data = _http_json(
            "GET",
            f"{base}?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"列出文档块失败: {data.get('msg') or data}")
        payload = data.get("data") or {}
        items.extend(payload.get("items") or [])
        page_token = str(payload.get("page_token") or "")
        if not payload.get("has_more") or not page_token:
            break
    return items


def _norm_for_match(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _locate_block(blocks: List[dict], needle: str) -> str:
    """
    找出包含 needle 的块，返回 block_id。

    命中多个块时**不猜**，直接报错并列出候选：评论会通知协作者，挂错位置
    比失败更糟。调用方可以改用更长的片段或直接传 block_id。
    """
    target = _norm_for_match(needle)
    if not target:
        raise RuntimeError("定位文字为空")
    exact: List[dict] = []
    partial: List[dict] = []
    for b in blocks:
        # 页面块（1）是文档根，整篇文字都在它下面，会误命中
        if b.get("block_type") == 1:
            continue
        text = _norm_for_match(_block_plain_text(b))
        if not text:
            continue
        if text == target:
            exact.append(b)
        elif target in text:
            partial.append(b)
    hits = exact or partial
    if not hits:
        raise RuntimeError(
            f"没找到包含「{needle}」的块；确认文字与文档一致（可只取一小段连续文字）"
        )
    if len(hits) > 1:
        preview = "；".join(
            f"{b.get('block_id')}=「{_block_plain_text(b)[:24]}」" for b in hits[:5]
        )
        raise RuntimeError(
            f"「{needle}」命中 {len(hits)} 个块，无法确定评论位置。"
            f"请换更独特的片段，或直接指定 block_id。候选：{preview}"
        )
    return str(hits[0].get("block_id") or "")


# update_text_elements 只认文本类块：表格、图片、分割线没有 elements 可改
_TEXT_BLOCK_TYPES = frozenset(_BLOCK_FIELD)


def docx_url(cfg: FeishuConfig, document_id: str) -> str:
    """文档可点链接；没配 doc_host 时返回空串（api_base 推不出企业域名）。"""
    return _docx_url(cfg, document_id)


def find_docx_block(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    needle: str,
    *,
    config_path: Optional[str] = None,
) -> Tuple[str, str]:
    """
    只读：按一段原文定位块，返回 (block_id, 该块当前全文)。命中多个或没命中都抛异常。

    局部评论自带 quote（用户划中的那句），拿它就能定位到块——这是评论机器人能
    「只改这一段」的前提。
    """
    if not feishu_configured(cfg):
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> Tuple[str, str]:
        document_id, _title = _resolve_document_id(api_base, token, ref, timeout)
        blocks = _all_blocks(api_base, token, document_id, timeout)
        block_id = _locate_block(blocks, needle)
        for b in blocks:
            if str(b.get("block_id") or "") == block_id:
                return block_id, _block_plain_text(b)
        return block_id, ""

    _tok, found = _with_user_token(cfg, config_path, _read)
    return found


def _fetch_block(
    api_base: str, access_token: str, document_id: str, block_id: str, timeout: float
) -> dict:
    doc = urllib.parse.quote(document_id, safe="")
    bid = urllib.parse.quote(block_id, safe="")
    data = _http_json(
        "GET",
        f"{api_base}/open-apis/docx/v1/documents/{doc}/blocks/{bid}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"读取块失败: {data.get('msg') or data}")
    return (data.get("data") or {}).get("block") or {}


def update_docx_block_text(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    block_id: str,
    new_text: str,
    *,
    expect_text: str = "",
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> FeishuBlockUpdateResult:
    """
    改一个块的文字（PATCH .../blocks/{block_id}，update_text_elements）。

    这是 update_docx_body 之外最小粒度的写法：只动指定的那一块，别处一个字不碰。
    评论机器人只允许这种改法 —— mode=replace 会删掉全文所有块，不能交给自动流程。

    `expect_text`：非空时先比对块的当前文字，对不上就拒绝执行。提案和落笔之间隔着
    人工确认，中间别人可能已经改过同一段，覆盖掉才是真正的事故。

    和其它写操作同一门禁：confirmed 必须显式传 True，否则不发任何请求。
    行内样式会被这次替换抹平（整块文字换成一段纯文本），所以只适合改一句话，
    不适合改带复杂格式的长段落。
    """
    if not confirmed:
        return FeishuBlockUpdateResult(
            url=ref.url,
            ok=False,
            block_id=block_id,
            error="未确认：改飞书文档正文需本人逐次确认，调用方须显式传 confirmed=True",
        )
    text = (new_text or "").strip()
    if not text:
        return FeishuBlockUpdateResult(
            url=ref.url, ok=False, block_id=block_id, error="新内容为空，不做改动"
        )
    if not block_id:
        return FeishuBlockUpdateResult(
            url=ref.url, ok=False, error="未指定 block_id，不知道要改哪一块"
        )
    if not feishu_configured(cfg):
        return FeishuBlockUpdateResult(
            url=ref.url, ok=False, block_id=block_id, error="未配置飞书 app_id / app_secret"
        )

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> Tuple[str, str, dict]:
        document_id, title = _resolve_document_id(api_base, token, ref, timeout)
        block = _fetch_block(api_base, token, document_id, block_id, timeout)
        return document_id, title, block

    try:
        token, (document_id, title, block) = _with_user_token(cfg, config_path, _read)
    except Exception as e:
        return FeishuBlockUpdateResult(
            url=ref.url, ok=False, block_id=block_id, error=str(e)
        )

    old_text = _block_plain_text(block)
    block_type = int(block.get("block_type") or 0)
    if block_type not in _TEXT_BLOCK_TYPES:
        return FeishuBlockUpdateResult(
            url=ref.url,
            ok=False,
            document_id=document_id,
            title=title,
            block_id=block_id,
            old_text=old_text,
            error=f"块类型 {block_type} 不是文本块，改不了（表格/图片这类要在飞书里手动改）",
        )
    if expect_text and _norm_for_match(expect_text) != _norm_for_match(old_text):
        return FeishuBlockUpdateResult(
            url=ref.url,
            ok=False,
            document_id=document_id,
            title=title,
            block_id=block_id,
            old_text=old_text,
            error="这段正文和提案时不一样了（可能已被别人改过），没有覆盖；请重新提一次",
        )

    doc = urllib.parse.quote(document_id, safe="")
    bid = urllib.parse.quote(block_id, safe="")
    try:
        data = _http_json(
            "PATCH",
            f"{api_base}/open-apis/docx/v1/documents/{doc}/blocks/{bid}",
            headers={"Authorization": f"Bearer {token}"},
            body={
                "update_text_elements": {
                    "elements": [
                        {"text_run": {"content": text, "text_element_style": {}}}
                    ]
                }
            },
            timeout=timeout,
        )
    except Exception as e:
        err = str(e)
        hint = ""
        if "1770032" in err or "131006" in err or "permission" in err.lower():
            hint = "；改正文需要该文档的编辑权限与 docx:document:write_only"
        return FeishuBlockUpdateResult(
            url=ref.url,
            ok=False,
            document_id=document_id,
            title=title,
            block_id=block_id,
            old_text=old_text,
            error=err + hint,
        )
    if data.get("code") != 0:
        return FeishuBlockUpdateResult(
            url=ref.url,
            ok=False,
            document_id=document_id,
            title=title,
            block_id=block_id,
            old_text=old_text,
            error=f"改正文失败: {data.get('msg') or data}",
        )
    return FeishuBlockUpdateResult(
        url=ref.url,
        ok=True,
        document_id=document_id,
        title=title,
        block_id=block_id,
        old_text=old_text,
        new_text=text,
    )


def _comment_text(content: Optional[dict]) -> str:
    """把评论 elements 拼成纯文本；@人与云文档链接也保留可读形式。"""
    out: List[str] = []
    for el in (content or {}).get("elements") or []:
        kind = el.get("type")
        if kind == "text_run":
            out.append(str((el.get("text_run") or {}).get("text") or ""))
        elif kind == "docs_link":
            out.append(str((el.get("docs_link") or {}).get("url") or ""))
        elif kind == "person":
            uid = str((el.get("person") or {}).get("user_id") or "")
            if uid:
                out.append(f"@{uid}")
    return "".join(out).strip()


def _parse_comment(item: dict) -> FeishuComment:
    reply_items = [
        FeishuCommentReply(
            reply_id=str(r.get("reply_id") or ""),
            user_id=str(r.get("user_id") or ""),
            created_at=str(r.get("create_time") or ""),
            text=_comment_text(r.get("content")),
        )
        for r in ((item.get("reply_list") or {}).get("replies") or [])
    ]
    return FeishuComment(
        comment_id=str(item.get("comment_id") or ""),
        user_id=str(item.get("user_id") or ""),
        created_at=str(item.get("create_time") or ""),
        is_whole=bool(item.get("is_whole")),
        is_solved=bool(item.get("is_solved")),
        quote=str(item.get("quote") or ""),
        replies=[r.text for r in reply_items if r.text],
        reply_items=reply_items,
    )


def list_docx_comments(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    *,
    config_path: Optional[str] = None,
    page_size: int = 50,
    max_comments: int = 200,
    resolve_title: bool = True,
) -> FeishuCommentListResult:
    """
    只读：分页拉取文档全部评论（含客户端里加的局部评论，看 is_whole / quote 区分）。

    评论只是读，不受写门禁限制。

    `resolve_title=False` 是给轮询用的：docx 取标题是额外一次网络往返，几十秒一轮、
    十几篇文档地打下去请求量白白翻倍，而轮询根本不看标题。
    """
    if not feishu_configured(cfg):
        return FeishuCommentListResult(
            url=ref.url, ok=False, error="未配置飞书 app_id / app_secret"
        )
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)
    size = max(1, min(int(page_size or 50), 100))
    cap = max(1, int(max_comments or 200))

    def _read(token: str) -> Tuple[str, str, List[FeishuComment], bool]:
        if resolve_title or ref.kind == "wiki":
            document_id, title = _resolve_document_id(api_base, token, ref, timeout)
        else:
            document_id, title = ref.token, ""
        path = urllib.parse.quote(document_id, safe="")
        items: List[FeishuComment] = []
        page_token = ""
        truncated = False
        while True:
            q = {"file_type": "docx", "page_size": size, "user_id_type": "open_id"}
            if page_token:
                q["page_token"] = page_token
            url = (
                f"{api_base}/open-apis/drive/v1/files/{path}/comments"
                f"?{urllib.parse.urlencode(q)}"
            )
            data = _http_json(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
            if data.get("code") != 0:
                raise RuntimeError(f"读评论失败: {data.get('msg') or data}")
            payload = data.get("data") or {}
            for it in payload.get("items") or []:
                items.append(_parse_comment(it))
                if len(items) >= cap:
                    # 到上限就停，别把几百条评论一次塞回去
                    return document_id, title, items, True
            page_token = str(payload.get("page_token") or "")
            if not payload.get("has_more") or not page_token:
                break
        return document_id, title, items, truncated

    try:
        _tok, (document_id, title, items, truncated) = _with_user_token(
            cfg, config_path, _read
        )
    except Exception as e:
        err = str(e)
        hint = ""
        if "1069303" in err or "permission" in err.lower() or "20027" in err:
            hint = "；读评论需要开通 docs:document.comment:read 并重新授权"
        return FeishuCommentListResult(url=ref.url, ok=False, error=err + hint)
    return FeishuCommentListResult(
        url=ref.url,
        ok=True,
        document_id=document_id,
        title=title,
        comments=items,
        truncated=truncated,
    )


DOC_COMMENT_EVENT = "drive.notice.comment_add_v1"


def subscribe_user_doc_events(
    cfg: FeishuConfig,
    *,
    config_path: Optional[str] = None,
    event_type: str = DOC_COMMENT_EVENT,
) -> None:
    """
    以本人身份订阅云文档事件，失败抛异常。

    这一步不做，长连接**一条评论事件都收不到**：`drive.notice.comment_add_v1` 是
    「某个用户收到评论通知」时才推送的，得先有人订阅。用户身份订阅只能靠
    user_access_token，且推送范围就是你自己在飞书里能收到通知的那些评论。

    接口幂等，每次启动调一次即可。需要 docs:event:subscribe + docs:document.comment:read。
    """
    if not feishu_configured(cfg):
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _post(token: str) -> bool:
        data = _http_json(
            "POST",
            f"{api_base}/open-apis/drive/v1/user/subscription",
            headers={"Authorization": f"Bearer {token}"},
            body={"event_type": event_type},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"订阅云文档事件失败: {data.get('msg') or data}")
        return True

    # 幂等，所以借 _with_user_token 的「token 过期就刷新重试」没有副作用
    _with_user_token(cfg, config_path, _post)


@dataclass
class FileSubscribeResult:
    url: str
    ok: bool
    identity: str = ""  # tenant / user
    document_id: str = ""
    error: str = ""


def subscribe_file_events(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    *,
    config_path: Optional[str] = None,
) -> FileSubscribeResult:
    """
    按文件订阅云文档事件，让这篇文档的评论能推到机器人。

    只做 `subscribe_user_doc_events`（用户维度）是不够的：那条链路的推送前提是
    「你本人在飞书客户端收到了这条评论通知」，而飞书**不会就你自己的评论通知你自己**，
    所以自己在文档里 @ 机器人永远收不到事件（2026-08-06 实测）。

    优先用应用身份（tenant）：应用订阅之后谁评论都推，包括你自己发的。应用身份需要
    在开放平台开通「应用身份」的 docs:event:subscribe，没开会报 99991672，
    这时退回用户身份——那样至少别人评论（会给你产生通知的那些）能触发。
    """
    if not feishu_configured(cfg):
        return FileSubscribeResult(url=ref.url, ok=False, error="未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)

    # wiki 链接的 token 不能直接喂给订阅接口，要先解析成 docx 的 obj_token
    try:
        token, (document_id, _title) = _with_user_token(
            cfg,
            config_path,
            lambda t: _resolve_document_id(api_base, t, ref, timeout),
        )
    except Exception as e:  # noqa: BLE001
        return FileSubscribeResult(url=ref.url, ok=False, error=str(e))

    url = f"{api_base}/open-apis/drive/v1/files/{document_id}/subscribe?file_type=docx"
    errors = []
    try:
        tenant = _tenant_access_token(api_base, app_id, app_secret, timeout)
        data = _http_json(
            "POST", url, headers={"Authorization": f"Bearer {tenant}"}, body={}, timeout=timeout
        )
        if data.get("code") == 0:
            return FileSubscribeResult(
                url=ref.url, ok=True, identity="tenant", document_id=document_id
            )
        errors.append(f"应用身份: {data.get('code')} {data.get('msg') or data}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"应用身份: {e}")

    try:
        data = _http_json(
            "POST", url, headers={"Authorization": f"Bearer {token}"}, body={}, timeout=timeout
        )
        if data.get("code") == 0:
            return FileSubscribeResult(
                url=ref.url, ok=True, identity="user", document_id=document_id
            )
        errors.append(f"用户身份: {data.get('code')} {data.get('msg') or data}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"用户身份: {e}")

    hint = ""
    if any("99991672" in x for x in errors):
        hint = (
            "；应用身份缺 docs:event:subscribe，去开放平台按报错里的链接开通后，"
            "自己发的评论才能触发机器人"
        )
    return FileSubscribeResult(url=ref.url, ok=False, error="；".join(errors) + hint)


def file_subscription_on(
    cfg: FeishuConfig,
    document_id: str,
    *,
    config_path: Optional[str] = None,
) -> bool:
    """查一篇文档当前是否已按文件订阅（查不到就当作没订阅）。"""
    if not feishu_configured(cfg) or not document_id:
        return False
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)
    url = f"{api_base}/open-apis/drive/v1/files/{document_id}/get_subscribe?file_type=docx"

    def _read(token: str) -> bool:
        data = _http_json("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        return bool((data.get("data") or {}).get("is_subscribe"))

    try:
        return _with_user_token(cfg, config_path, _read)[1]
    except Exception:  # noqa: BLE001 - 查不到不该挡住调用方
        return False


def get_file_comment(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    comment_id: str,
    *,
    config_path: Optional[str] = None,
) -> FeishuCommentListResult:
    """
    只读：按 comment_id 精确取一条评论（含 quote 与全部回复）。

    评论事件只带 comment_id、不带正文。用 list_docx_comments 拉全量再筛，一篇几百条
    评论的文档每来一条评论就要翻好几页，所以走 batch_query 只取这一条。
    """
    if not comment_id:
        return FeishuCommentListResult(url=ref.url, ok=False, error="未指定 comment_id")
    if not feishu_configured(cfg):
        return FeishuCommentListResult(
            url=ref.url, ok=False, error="未配置飞书 app_id / app_secret"
        )
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> Tuple[str, str, List[FeishuComment]]:
        document_id, title = _resolve_document_id(api_base, token, ref, timeout)
        path = urllib.parse.quote(document_id, safe="")
        query = urllib.parse.urlencode({"file_type": "docx", "user_id_type": "open_id"})
        data = _http_json(
            "POST",
            f"{api_base}/open-apis/drive/v1/files/{path}/comments/batch_query?{query}",
            headers={"Authorization": f"Bearer {token}"},
            body={"comment_ids": [comment_id]},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"读评论失败: {data.get('msg') or data}")
        items = [_parse_comment(it) for it in (data.get("data") or {}).get("items") or []]
        return document_id, title, items

    try:
        _tok, (document_id, title, items) = _with_user_token(cfg, config_path, _read)
    except Exception as e:
        err = str(e)
        hint = ""
        if "1069303" in err or "permission" in err.lower() or "20027" in err:
            hint = "；读评论需要开通 docs:document.comment:read 并重新授权"
        return FeishuCommentListResult(url=ref.url, ok=False, error=err + hint)
    if not items:
        return FeishuCommentListResult(
            url=ref.url,
            ok=False,
            document_id=document_id,
            title=title,
            error=f"没查到评论 {comment_id}（可能已被删除）",
        )
    return FeishuCommentListResult(
        url=ref.url, ok=True, document_id=document_id, title=title, comments=items
    )


def _comment_error_hint(err: str, replying: bool) -> str:
    """把飞书的评论错误码翻成「该去改什么」。"""
    if "1069303" in err or "permission" in err.lower() or "20027" in err:
        return f"{err}；加评论需要开通 docs:document.comment:create 并重新授权"
    if replying and "1069302" in err:
        return f"{err}；已解决的评论不支持回复（先在飞书里取消解决）"
    return err


def _escape_comment_text(text: str) -> str:
    """new_comments 接口不接受裸 < >，要先转义，否则内容会被拒或截断。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 评论表情用的是另一套枚举，不能直接拿 IM 那三个（OnIt / DONE 不在云文档的可选值里）
DOC_REACTION_WORKING = "Typing"
DOC_REACTION_DONE = "CheckMark"
DOC_REACTION_FAILED = "CrossMark"


def _app_token_or_empty(cfg: FeishuConfig) -> str:
    """
    应用身份的 token；拿不到就返回空串让调用方退回本人身份。

    评论类接口两种 token 都收，但**署名跟着 token 走**：用 tenant 发出去在文档里
    就是机器人，用 user 发出去署的是你本人的名字。所以能拿到应用身份就优先用它。
    """
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    if not (app_id and app_secret):
        return ""
    try:
        return _tenant_access_token(api_base, app_id, app_secret, float(cfg.timeout or 30))
    except Exception:  # noqa: BLE001 - 退回本人身份即可，不该让整次调用失败
        return ""


def update_comment_reaction(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    reply_id: str,
    reaction_type: str,
    *,
    action: str = "add",
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> bool:
    """
    给文档评论里的某条回复贴 / 撤表情。贴上了返回 True，没开门禁 / 没配置返回 False，
    请求真失败则抛异常——调用方要靠错误码分辨「权限没开」和「网络抖了」。

    操作对象是 **reply_id 而不是 comment_id**：一条评论卡片下面挂着一串回复，
    表情是挂在具体某条回复上的。走 v2 的 comments/reaction 单接口，用 action 区分
    add / delete，所以撤销时不需要记什么 reaction_id（与 IM 那套不同）。

    **优先用应用身份贴**，这样文档里显示的是机器人而不是你本人；应用对这篇文档没权限
    时退回本人身份，至少表情还在。

    和其它飞书写操作同一门禁：confirmed 必须显式传 True。表情比评论轻得多（不发通知），
    但协作者一样看得见，不给绕过路径。
    """
    if not confirmed:
        return False
    if not reply_id or not reaction_type:
        return False
    if not feishu_configured(cfg):
        return False
    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)
    body = {"action": action, "reply_id": reply_id, "reaction_type": reaction_type}

    def _post(token: str, document_id: str) -> dict:
        path = urllib.parse.quote(document_id, safe="")
        query = urllib.parse.urlencode({"file_type": "docx"})
        return _http_json(
            "POST",
            f"{api_base}/open-apis/drive/v2/files/{path}/comments/reaction?{query}",
            headers={"Authorization": f"Bearer {token}"},
            body=body,
            timeout=timeout,
        )

    def _run(token: str) -> bool:
        # 定位文档只能用 user token（wiki 节点解析走的是本人权限），署名只由发表情
        # 那一次请求的 token 决定，所以这里读用 user、写优先 tenant
        document_id, _title = _resolve_document_id(api_base, token, ref, timeout)
        app_token = _app_token_or_empty(cfg)
        if app_token:
            try:
                data = _post(app_token, document_id)
                if data.get("code") == 0:
                    return True
            except Exception:  # noqa: BLE001 - 应用没这篇文档的权限就退回本人身份
                pass
        data = _post(token, document_id)
        if data.get("code") != 0:
            raise RuntimeError(f"{data.get('msg') or data}")
        return True

    _tok, ok = _with_user_token(cfg, config_path, _run)
    return bool(ok)


def create_docx_comment(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    text: str,
    *,
    comment_id: str = "",
    block_id: str = "",
    anchor_text: str = "",
    config_path: Optional[str] = None,
    confirmed: bool = False,
    as_app: bool = False,
) -> FeishuCommentResult:
    """
    给文档加评论。三种模式：

    - 默认：全文评论，显示在文档底部
    - 传 comment_id：回复已有评论
    - 传 block_id 或 anchor_text：**局部评论（划词评论）**，锚定到具体块，
      在正文旁边显示并带引用

    局部评论走 v2 的 new_comments 接口（anchor.block_id）；v1 的 comments 接口
    只能建全文评论，传 is_whole / quote 会被静默忽略。anchor_text 会先列出所有
    块按文字定位，命中多个就报错而不是猜。

    `as_app=True` 用应用身份发，文档里署名就是机器人（评论机器人走这条）；应用对这篇
    文档没权限时自动退回本人身份，不会因此发不出去。默认仍是本人身份——AI 代你做文档
    评审时那些意见署你的名，是你在说话。

    评论会通知文档协作者、别人立刻看得见，所以和改正文同一门禁：
    confirmed 必须显式传 True，否则直接返回错误且不发任何请求。
    """
    if not confirmed:
        return FeishuCommentResult(
            url=ref.url,
            ok=False,
            error="未确认：评论飞书文档需本人逐次确认，调用方须显式传 confirmed=True",
        )
    body_text = (text or "").strip()
    if not body_text:
        return FeishuCommentResult(url=ref.url, ok=False, error="评论内容为空，不做改动")
    if not feishu_configured(cfg):
        return FeishuCommentResult(
            url=ref.url, ok=False, error="未配置飞书 app_id / app_secret"
        )
    if comment_id and (block_id or anchor_text):
        return FeishuCommentResult(
            url=ref.url,
            ok=False,
            error="回复已有评论与新建局部评论不能同时指定",
        )

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> Tuple[str, str, str]:
        document_id, title = _resolve_document_id(api_base, token, ref, timeout)
        anchor = block_id
        if anchor_text and not anchor:
            anchor = _locate_block(
                _all_blocks(api_base, token, document_id, timeout), anchor_text
            )
        return document_id, title, anchor

    # 先用只读步骤定位文档（必要时定位块）并验证 token，再用同一个 token 发评论，
    # 避免评论发出去才发现 token 过期、重试又评论两遍
    try:
        token, (document_id, title, anchor_block) = _with_user_token(
            cfg, config_path, _read
        )
    except Exception as e:
        return FeishuCommentResult(url=ref.url, ok=False, error=str(e))

    path = urllib.parse.quote(document_id, safe="")
    query = urllib.parse.urlencode({"file_type": "docx"})
    payload: Dict
    if comment_id:
        # 回复必须走 comments/{id}/replies。往 comments 接口的 body 里塞 comment_id
        # 是**没用的**——那个字段只在响应里有，请求体规范里没有，会被静默忽略，
        # 于是每次都在文档底部新建一条全文评论，看着像「机器人不回到串里」
        cid = urllib.parse.quote(comment_id, safe="")
        url = f"{api_base}/open-apis/drive/v1/files/{path}/comments/{cid}/replies?{query}"
        payload = {
            "content": {"elements": [{"type": "text_run", "text_run": {"text": body_text}}]}
        }
    elif anchor_block:
        url = f"{api_base}/open-apis/drive/v1/files/{path}/new_comments?{query}"
        payload = {
            "file_type": "docx",
            "reply_elements": [
                {"type": "text", "text": _escape_comment_text(body_text)}
            ],
            "anchor": {"block_id": anchor_block},
        }
    else:
        url = f"{api_base}/open-apis/drive/v1/files/{path}/comments?{query}"
        payload = {
            "reply_list": {
                "replies": [
                    {
                        "content": {
                            "elements": [
                                {"type": "text_run", "text_run": {"text": body_text}}
                            ]
                        }
                    }
                ]
            }
        }

    def _send(access_token: str) -> dict:
        return _http_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            body=payload,
            timeout=timeout,
        )

    data: Optional[Dict] = None
    if as_app:
        app_token = _app_token_or_empty(cfg)
        if app_token:
            try:
                sent = _send(app_token)
                data = sent if sent.get("code") == 0 else None
            except Exception:  # noqa: BLE001 - 应用没这篇文档的权限就退回本人身份
                data = None
    if data is None:
        try:
            data = _send(token)
        except Exception as e:
            return FeishuCommentResult(
                url=ref.url,
                ok=False,
                document_id=document_id,
                title=title,
                error=_comment_error_hint(str(e), bool(comment_id)),
            )
        if data.get("code") != 0:
            return FeishuCommentResult(
                url=ref.url,
                ok=False,
                document_id=document_id,
                title=title,
                error=_comment_error_hint(
                    f"加评论失败: {data.get('msg') or data}", bool(comment_id)
                ),
            )
    payload_data = data.get("data") or {}
    # 回复接口返回的是 reply_id；评论串还是原来那条，所以 comment_id 沿用入参
    new_reply = str(payload_data.get("reply_id") or "")
    new_id = str(payload_data.get("comment_id") or "") or comment_id
    return FeishuCommentResult(
        url=ref.url,
        ok=True,
        document_id=document_id,
        title=title,
        comment_id=new_id,
        replied_to=comment_id,
        reply_id=new_reply,
        block_id=anchor_block,
    )


def fetch_bot_message(cfg: FeishuConfig, message_id: str) -> Optional[Tuple[str, str]]:
    """
    按 message_id 拉一条消息，返回 (msg_type, content)；拿不到返回 None。

    用来读「用户回复的那一条」——事件里只给 parent_id，正文得自己取回来。
    群里读别人的消息需要 im:message.group_msg 权限，没开通时飞书返回 230027。
    """
    if not message_id:
        return None
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    token = _tenant_access_token(api_base, app_id, app_secret, timeout)
    url = f"{api_base}/open-apis/im/v1/messages/{message_id}"
    try:
        data = _http_json(
            "GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if "230027" in err or "99991672" in err:
            raise RuntimeError(
                f"{err}；读群里被引用的消息需要开通 im:message.group_msg 权限并发布版本"
            ) from e
        raise
    if data.get("code") != 0:
        raise RuntimeError(f"读消息失败: {data.get('msg') or data}")
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return None
    item = items[0] or {}
    body = item.get("body") or {}
    return str(item.get("msg_type") or ""), str(body.get("content") or "")


def fetch_bot_identity(cfg: FeishuConfig) -> Tuple[str, str]:
    """
    机器人自己的 (open_id, 名字)。群里判断「这句是不是 @ 我」要用。

    事件里的 mentions 只给被 @ 者的 open_id 和显示名，而配置里只有 app_id，
    两者对不上，所以得先问一次飞书自己是谁。
    """
    app_id, app_secret, _user, api_base = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    token = _tenant_access_token(api_base, app_id, app_secret, timeout)
    data = _http_json(
        "GET",
        f"{api_base}/open-apis/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"读机器人信息失败: {data.get('msg') or data}")
    bot = data.get("bot") or (data.get("data") or {}).get("bot") or {}
    return str(bot.get("open_id") or ""), str(bot.get("app_name") or "")


def fetch_message_as_user(
    cfg: FeishuConfig,
    message_id: str,
    *,
    config_path: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    以**用户身份**读一条消息，返回 (msg_type, content)；拿不到返回 None。

    应用身份读别的应用发的卡片只能拿到 150~200 字节的摘要外壳（一个 image_key 加空文本），
    实测同一个群里用户自己发的卡片则有完整正文——卡住的是跨应用。用户在客户端里看得见全文，
    所以换用户身份还有一次机会。需要授权 im:message:readonly，没授权时飞书返回 99991679。
    """
    if not message_id:
        return None
    _app_id, _secret, _user, api_base = _resolve_credentials(cfg)
    timeout = float(cfg.timeout or 30)
    url = f"{api_base}/open-apis/im/v1/messages/{message_id}"

    def _read(token: str) -> dict:
        return _http_json(
            "GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )

    try:
        _tok, data = _with_user_token(cfg, config_path, _read)
    except Exception as e:  # noqa: BLE001 - 缺权限是最常见的失败，得说清怎么补
        err = str(e)
        if "99991679" in err:
            raise RuntimeError(
                f"{err}；以用户身份读消息需要 im:message:readonly，"
                "在开放平台后台补上这项用户身份权限后重跑 python3 scripts/feishu_login.py"
            ) from e
        raise
    if data.get("code") != 0:
        raise RuntimeError(f"读消息失败: {data.get('msg') or data}")
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return None
    item = items[0] or {}
    body = item.get("body") or {}
    return str(item.get("msg_type") or ""), str(body.get("content") or "")


def list_chat_messages(
    cfg: FeishuConfig,
    chat_id: str,
    *,
    before_message_id: str = "",
    limit: int = 12,
    max_pages: int = 4,
) -> List[Dict[str, str]]:
    """
    读群里的一段历史，按时间**正序**返回 [{msg_type, content, sender_type}]。

    用途是「被引用的那条读不出字」时去上游找料。实测过一次典型现场：Slardar 的结论
    卡片是 157 字节空壳，但它上游第 6 条——**人转发进群的**告警卡片——有 6541 字节、
    1115 个可读字，接口照给。别的机器人能就同一张卡片给出结论，靠的就是这段，
    不是什么高级权限。

    `before_message_id` 给的是被引用那条，只取它**之前**的消息：料在上游，
    它后面多半是本轮的追问和自己的回复。接口只能按时间倒序整页翻，所以翻到那条
    为止（最多 max_pages 页），再往前数 limit 条。翻不到就退回最近 limit 条。

    走应用身份：同一个群实测应用身份能拿 25 条、用户身份只有 6 条（用户身份只给
    本人可见的部分）。需要 im:message.group_msg，没开时飞书返回 230027。
    """
    if not chat_id:
        return []
    app_id, app_secret, _user, api_base = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    token = _tenant_access_token(api_base, app_id, app_secret, timeout)
    page_size = max(limit, 20)

    newest_first: List[dict] = []
    page_token = ""
    for _ in range(max(1, max_pages)):
        url = (
            f"{api_base}/open-apis/im/v1/messages?container_id_type=chat"
            f"&container_id={urllib.parse.quote(chat_id, safe='')}"
            f"&sort_type=ByCreateTimeDesc&page_size={page_size}"
        )
        if page_token:
            url += f"&page_token={urllib.parse.quote(page_token, safe='')}"
        try:
            data = _http_json(
                "GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
            )
        except Exception as e:  # noqa: BLE001
            err = str(e)
            if "230027" in err or "99991672" in err:
                raise RuntimeError(
                    f"{err}；读群历史需要开通 im:message.group_msg 权限并发布版本"
                ) from e
            raise
        if data.get("code") != 0:
            raise RuntimeError(f"读群历史失败: {data.get('msg') or data}")
        payload = data.get("data") or {}
        newest_first.extend(x for x in (payload.get("items") or []) if isinstance(x, dict))
        anchor = _index_of_message(newest_first, before_message_id)
        # 锚点后面还得留够 limit 条才算够用，否则继续往前翻
        if anchor >= 0 and len(newest_first) - anchor - 1 >= limit:
            break
        if not payload.get("has_more"):
            break
        page_token = str(payload.get("page_token") or "")
        if not page_token:
            break

    anchor = _index_of_message(newest_first, before_message_id)
    window = newest_first[anchor + 1 : anchor + 1 + limit] if anchor >= 0 else newest_first[:limit]

    out: List[Dict[str, str]] = []
    for item in reversed(window):  # 倒序翻页拿到的是新→旧，模型要读的是旧→新
        sender = item.get("sender") or {}
        # 自己说过的话不算上下文：机器人的帮助文本、上一轮回复混进去只会带偏模型
        if str(sender.get("id") or "") == app_id:
            continue
        body = item.get("body") or {}
        out.append(
            {
                "msg_type": str(item.get("msg_type") or ""),
                "content": str(body.get("content") or ""),
                "sender_type": str(sender.get("sender_type") or ""),
            }
        )
    return out


def _index_of_message(items: Sequence[dict], message_id: str) -> int:
    if not message_id:
        return -1
    for i, item in enumerate(items):
        if str(item.get("message_id") or "") == message_id:
            return i
    return -1


# 机器人的「进度条」：开工贴 OnIt，收工换成 DONE，出错换成 CrossMark。
# 大小写照抄飞书 emoji_type 文档（OnIt 与 DONE 不是一个风格，但都得原样传）
REACTION_WORKING = "OnIt"
REACTION_DONE = "DONE"
REACTION_FAILED = "CrossMark"


def _reaction_error(err: str) -> str:
    if "231002" in err or "231008" in err or "99991672" in err:
        return f"{err}；机器人贴表情需要开通 im:message.reactions:write_only 权限并发布版本"
    return err


def add_message_reaction(cfg: FeishuConfig, message_id: str, emoji_type: str) -> str:
    """给一条消息贴表情回复，返回 reaction_id（删除时要用）。"""
    if not message_id:
        raise ValueError("message_id 不能为空")
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    token = _tenant_access_token(api_base, app_id, app_secret, timeout)
    url = f"{api_base}/open-apis/im/v1/messages/{message_id}/reactions"
    try:
        data = _http_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {token}"},
            body={"reaction_type": {"emoji_type": emoji_type}},
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_reaction_error(str(e))) from e
    if data.get("code") != 0:
        raise RuntimeError(_reaction_error(f"加表情失败: {data.get('msg') or data}"))
    return str((data.get("data") or {}).get("reaction_id") or "")


def remove_message_reaction(cfg: FeishuConfig, message_id: str, reaction_id: str) -> None:
    """撤掉之前贴的表情回复；reaction_id 来自 add_message_reaction。"""
    if not (message_id and reaction_id):
        return
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    token = _tenant_access_token(api_base, app_id, app_secret, timeout)
    url = f"{api_base}/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}"
    try:
        data = _http_json(
            "DELETE", url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_reaction_error(str(e))) from e
    if data.get("code") != 0:
        raise RuntimeError(_reaction_error(f"删表情失败: {data.get('msg') or data}"))


def send_bot_text(
    cfg: FeishuConfig,
    text: str,
    *,
    reply_to: str = "",
    chat_id: str = "",
) -> str:
    """
    以机器人身份发一条纯文本，返回 message_id。

    走 tenant_access_token：机器人说话是应用身份，与读文档用的 user token 无关，
    所以这里不碰 OAuth 那套续期逻辑。传 reply_to 则回复到原消息（带引用）。
    """
    if not (reply_to or chat_id):
        raise ValueError("reply_to 与 chat_id 至少要有一个")
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书 app_id / app_secret")
    timeout = float(cfg.timeout or 30)
    token = _tenant_access_token(api_base, app_id, app_secret, timeout)
    headers = {"Authorization": f"Bearer {token}"}
    content = json.dumps({"text": text or ""}, ensure_ascii=False)

    if reply_to:
        url = f"{api_base}/open-apis/im/v1/messages/{reply_to}/reply"
        body = {"content": content, "msg_type": "text"}
    else:
        url = f"{api_base}/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = {"receive_id": chat_id, "content": content, "msg_type": "text"}

    try:
        data = _http_json("POST", url, headers=headers, body=body, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        err = str(e)
        if "99991672" in err or "20027" in err or "permission" in err.lower():
            raise RuntimeError(
                f"{err}；机器人发消息需要开通 im:message:send_as_bot 权限并发布版本"
            ) from e
        raise
    if data.get("code") != 0:
        raise RuntimeError(f"发消息失败: {data.get('msg') or data}")
    return str((data.get("data") or {}).get("message_id") or "")
