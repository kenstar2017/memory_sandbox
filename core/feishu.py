"""飞书/Lark 文档读取（记忆沙箱内置，不依赖 Cursor/Trae MCP）。"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

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
    mode: str = ""
    blocks_written: int = 0
    blocks_deleted: int = 0
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


def fetch_feishu_document(
    cfg: FeishuConfig,
    ref: FeishuDocRef,
    *,
    config_path: Optional[str] = None,
) -> FeishuFetchResult:
    """拉取单篇飞书文档纯文本。"""
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


def _text_block(block_type: int, content: str) -> dict:
    return {
        "block_type": block_type,
        _BLOCK_FIELD[block_type]: {
            "elements": [{"text_run": {"content": content}}],
            "style": {},
        },
    }


def markdown_to_docx_blocks(text: str) -> List[dict]:
    """
    把 Markdown 子集转成 docx block 列表。

    支持 #~###### 标题、- / * 无序列表、1. 有序列表、``` 代码块、> 引用、--- 分割线，
    其余非空行作普通段落。行内语法（粗体、链接）不解析，按纯文本写入。
    """
    blocks: List[dict] = []
    code_lines: List[str] = []
    in_code = False
    for raw in (text or "").splitlines():
        if _FENCE_RE.match(raw):
            if in_code:
                blocks.append(_text_block(_BLOCK_CODE, "\n".join(code_lines)))
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(raw)
            continue
        line = raw.strip()
        if not line:
            continue
        if _DIVIDER_RE.match(line):
            blocks.append({"block_type": _BLOCK_DIVIDER, "divider": {}})
            continue
        m = _HEADING_RE.match(line)
        if m:
            blocks.append(
                _text_block(_HEADING_BLOCK[len(m.group(1))], m.group(2).strip())
            )
            continue
        m = _BULLET_RE.match(line)
        if m:
            blocks.append(_text_block(_BLOCK_BULLET, m.group(1).strip()))
            continue
        m = _ORDERED_RE.match(line)
        if m:
            blocks.append(_text_block(_BLOCK_ORDERED, m.group(1).strip()))
            continue
        m = _QUOTE_RE.match(line)
        if m:
            blocks.append(_text_block(_BLOCK_QUOTE, m.group(1).strip()))
            continue
        blocks.append(_text_block(_BLOCK_TEXT, line))
    # 围栏没闭合也不要丢内容
    if in_code and code_lines:
        blocks.append(_text_block(_BLOCK_CODE, "\n".join(code_lines)))
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


def _append_docx_blocks(
    api_base: str,
    access_token: str,
    document_id: str,
    blocks: List[dict],
    timeout: float,
) -> int:
    """把 block 分批追加到文档根节点，返回已写入数量。"""
    # 根节点的 block_id 就是 document_id
    path = urllib.parse.quote(document_id, safe="")
    url = f"{api_base}/open-apis/docx/v1/documents/{path}/blocks/{path}/children"
    written = 0
    for start in range(0, len(blocks), _BLOCK_BATCH):
        batch = blocks[start : start + _BLOCK_BATCH]
        if start:
            time.sleep(_BLOCK_WRITE_INTERVAL)
        data = _http_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            body={"index": -1, "children": batch},
            timeout=timeout,
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"写入正文失败（已写 {written} 块）: {data.get('msg') or data}"
            )
        written += len(batch)
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
            return "；创建/写入需应用开通 docx:document，指定文件夹时还需该文件夹的编辑权限"
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

    def _read(token: str) -> Tuple[str, int]:
        document_id, _title = _resolve_document_id(api_base, token, ref, timeout)
        existing = 0
        if mode == "replace":
            existing = len(_root_children(api_base, token, document_id, timeout))
        return document_id, existing

    # 先用只读步骤定位文档并验证 token，写操作再用同一个 token，
    # 避免删到一半才发现过期、重试又重复删
    try:
        token, (document_id, existing) = _with_user_token(cfg, config_path, _read)
    except Exception as e:
        err = str(e)
        hint = ""
        if "1770032" in err or "131006" in err or "permission" in err.lower():
            hint = "；改正文需要该文档的编辑权限，并确认应用已开通 docx:document"
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
            mode=mode,
            blocks_deleted=deleted,
            error=f"写入正文失败：{e}{tail}",
        )

    return FeishuBodyUpdateResult(
        url=ref.url,
        ok=True,
        document_id=document_id,
        mode=mode,
        blocks_written=written,
        blocks_deleted=deleted,
    )
