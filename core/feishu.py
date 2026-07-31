"""飞书/Lark 文档读取（记忆沙箱内置，不依赖 Cursor/Trae MCP）。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

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


def _wiki_obj_token(api_base: str, access_token: str, wiki_token: str, timeout: float) -> str:
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
    obj = node.get("obj_token") or ""
    if not obj:
        raise RuntimeError("未能获取文档 obj_token（可能无权限或 token 无效）")
    return obj


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

    # 过期则用 refresh_token 动态续期
    try:
        from .feishu_oauth import ensure_user_access_token

        ensure_user_access_token(cfg, config_path=config_path)
    except Exception:
        pass

    app_id, app_secret, user_token, api_base = _resolve_credentials(cfg)
    timeout = float(cfg.timeout or 30)
    try:
        tokens = _auth_tokens_to_try(api_base, app_id, app_secret, user_token, timeout)
    except Exception as e:
        return FeishuFetchResult(url=ref.url, ok=False, error=f"获取应用凭证失败: {e}")

    def _read_with(access_token: str) -> Tuple[str, str]:
        document_id = ref.token
        if ref.kind == "wiki":
            document_id = _wiki_obj_token(api_base, access_token, ref.token, timeout)
        content = _docx_raw_content(api_base, access_token, document_id, timeout)
        return document_id, content

    errors: List[str] = []
    refreshed_once = False
    for label, access_token in tokens:
        try:
            document_id, content = _read_with(access_token)
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
                title=ref.token,
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
                and ("99991668" in err or "Invalid access token" in err)
            ):
                try:
                    from .feishu_oauth import ensure_user_access_token

                    new_tok = ensure_user_access_token(
                        cfg, config_path=config_path, force_refresh=True
                    )
                    refreshed_once = True
                    document_id, content = _read_with(new_tok)
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
                            title=ref.token,
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
            blocks.append(
                f"### 飞书文档 {r.url}\n"
                f"(document_id={r.document_id})\n\n{r.content}"
            )
        else:
            blocks.append(f"### 飞书文档 {r.url}\n读取失败：{r.error}")
    return results, "\n\n".join(blocks)
