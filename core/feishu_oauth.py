"""飞书用户 OAuth：浏览器授权动态获取 user_access_token / refresh_token。

user_access_token 不能在管理后台查看明文，只能通过授权码换取，并用 refresh_token 续期。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from .config import FeishuConfig
from .feishu import _http_json, _resolve_credentials
from .paths import app_support_dir, default_config_path

DEFAULT_REDIRECT = "http://127.0.0.1:18765/feishu/callback"
DEFAULT_SCOPES = (
    "offline_access "
    "docs:document.content:read "
    "wiki:wiki:readonly "
    "wiki:node:read "
    "wiki:node:update "
    # 云文档写能力：API 文档统称 docx:document，但开放平台后台只能勾这三项细分权限，
    # 分别对应「创建文档」「获取所有子块」「创建块 / 删除块」
    "docx:document:create "
    "docx:document:readonly "
    "docx:document:write_only"
)

# 已被开放平台拆分、后台再也勾不到的聚合权限。请求它会让整个授权页报
# 20027「当前应用权限不足」，所以必须从旧配置里剔掉，而不是跟着并进去。
_RETIRED_SCOPES = {"docx:document"}


def _merged_scopes(cfg: FeishuConfig) -> str:
    """
    用户配置的 scope 与内置必需 scope 取并集，并剔除已废弃的聚合权限。

    用户配置会覆盖默认值，所以新增权限（如 wiki:node:update）时只改
    DEFAULT_SCOPES 不够——旧配置会把它挤掉，授权后仍然不可写。
    """
    parts = (cfg.oauth_scope or "").split() + DEFAULT_SCOPES.split()
    merged: list[str] = []
    for p in parts:
        if p and p not in merged and p not in _RETIRED_SCOPES:
            merged.append(p)
    return " ".join(merged)


def persist_feishu_auth(
    *,
    user_access_token: str = "",
    refresh_token: str = "",
    expires_in: int = 0,
    config_path: Optional[str] = None,
    enabled: bool = True,
) -> str:
    """把 token 写入用户配置（Application Support），返回路径。"""
    # 密钥永远写用户目录，避免进 git
    path = app_support_dir() / "config.yaml"
    if config_path:
        candidate = Path(config_path)
        if candidate.is_file() and "Application Support" in str(candidate):
            path = candidate

    raw: Dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feishu = dict(raw.get("feishu") or {})
    if enabled:
        feishu["enabled"] = True
    if user_access_token:
        feishu["user_access_token"] = user_access_token
    if refresh_token:
        feishu["refresh_token"] = refresh_token
    if expires_in:
        feishu["user_token_expires_at"] = int(time.time()) + int(expires_in) - 60
    raw["feishu"] = feishu
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(path)


def apply_tokens_to_config(
    cfg: FeishuConfig,
    *,
    user_access_token: str = "",
    refresh_token: str = "",
    expires_in: int = 0,
) -> None:
    if user_access_token:
        cfg.user_access_token = user_access_token
    if refresh_token:
        cfg.refresh_token = refresh_token
    if expires_in:
        cfg.user_token_expires_at = int(time.time()) + int(expires_in) - 60


def build_authorize_url(cfg: FeishuConfig, *, state: str = "") -> str:
    app_id, _, _, _ = _resolve_credentials(cfg)
    if not app_id:
        raise RuntimeError("缺少 feishu.app_id")
    redirect = (cfg.redirect_uri or DEFAULT_REDIRECT).strip()
    scope = _merged_scopes(cfg)
    # 与 @byted/mcp-lark-docs 一致：accounts 域 authorize
    # response_type 必填：缺失时飞书可能忽略 scope，导致换票不下发 refresh_token
    params = {
        "client_id": app_id,
        "redirect_uri": redirect,
        "scope": scope,
        "response_type": "code",
    }
    if state:
        params["state"] = state
    q = urllib.parse.urlencode(params)
    return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{q}"


def exchange_code_for_token(cfg: FeishuConfig, code: str) -> Dict[str, Any]:
    """授权码换 user_access_token（优先 oauth v2，失败再试经典 oidc）。"""
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    redirect = (cfg.redirect_uri or DEFAULT_REDIRECT).strip()
    timeout = float(cfg.timeout or 30)
    errors = []

    # 新版 OAuth v2
    try:
        data = _http_json(
            "POST",
            f"{api_base}/open-apis/authen/v2/oauth/token",
            body={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect,
            },
            timeout=timeout,
        )
        # v2 成功时可能无顶层 code，或 code==0
        if data.get("access_token") or (data.get("code") == 0 and data.get("access_token")):
            return data
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            return data["data"]
        errors.append(f"oauth/v2: {data.get('error_description') or data.get('msg') or data}")
    except Exception as e:
        errors.append(f"oauth/v2: {e}")

    # 经典：app_access_token + oidc/access_token
    try:
        app_token = _app_access_token(api_base, app_id, app_secret, timeout)
        data = _http_json(
            "POST",
            f"{api_base}/open-apis/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            body={"grant_type": "authorization_code", "code": code},
            timeout=timeout,
        )
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            return data["data"]
        errors.append(f"oidc: {data.get('msg') or data}")
    except Exception as e:
        errors.append(f"oidc: {e}")

    raise RuntimeError("授权码换 token 失败：" + "；".join(errors))


def refresh_user_access_token(cfg: FeishuConfig) -> Dict[str, Any]:
    """用 refresh_token 换新的 user_access_token（一次性，会返回新 refresh_token）。"""
    app_id, app_secret, _, api_base = _resolve_credentials(cfg)
    refresh = (cfg.refresh_token or "").strip()
    if not refresh:
        raise RuntimeError(
            "没有 refresh_token。请先运行：python3 scripts/feishu_login.py（浏览器授权，勿手抄 token）"
        )
    timeout = float(cfg.timeout or 30)
    errors = []

    try:
        data = _http_json(
            "POST",
            f"{api_base}/open-apis/authen/v2/oauth/token",
            body={
                "grant_type": "refresh_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "refresh_token": refresh,
            },
            timeout=timeout,
        )
        if data.get("access_token"):
            return data
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            return data["data"]
        errors.append(f"oauth/v2: {data.get('error_description') or data.get('msg') or data}")
    except Exception as e:
        errors.append(f"oauth/v2: {e}")

    try:
        app_token = _app_access_token(api_base, app_id, app_secret, timeout)
        data = _http_json(
            "POST",
            f"{api_base}/open-apis/authen/v1/oidc/refresh_access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            body={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=timeout,
        )
        if data.get("code") == 0 and isinstance(data.get("data"), dict):
            return data["data"]
        errors.append(f"oidc refresh: {data.get('msg') or data}")
    except Exception as e:
        errors.append(f"oidc refresh: {e}")

    raise RuntimeError("刷新 user_access_token 失败：" + "；".join(errors))


def _app_access_token(api_base: str, app_id: str, app_secret: str, timeout: float) -> str:
    data = _http_json(
        "POST",
        f"{api_base}/open-apis/auth/v3/app_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
        timeout=timeout,
    )
    token = data.get("app_access_token") or ""
    if not token:
        # 部分应用返回 tenant_access_token 亦可作 app 凭证场景
        token = data.get("tenant_access_token") or ""
    if not token:
        raise RuntimeError(f"获取 app_access_token 失败: {data}")
    return token


def ensure_user_access_token(
    cfg: FeishuConfig,
    *,
    config_path: Optional[str] = None,
    force_refresh: bool = False,
) -> str:
    """
    确保内存中的 cfg 持有可用 user_access_token。
    过期或 force_refresh 时用 refresh_token 刷新并落盘。
    """
    now = int(time.time())
    token = (cfg.user_access_token or "").strip()
    exp = int(getattr(cfg, "user_token_expires_at", 0) or 0)
    need = force_refresh or (not token) or (exp and now >= exp)
    if not need:
        return token
    if not (cfg.refresh_token or "").strip():
        if token and not force_refresh:
            return token
        raise RuntimeError(
            "user_access_token 无效/过期且无 refresh_token。"
            "请执行：python3 scripts/feishu_login.py"
        )
    data = refresh_user_access_token(cfg)
    access = data.get("access_token") or data.get("user_access_token") or ""
    new_refresh = data.get("refresh_token") or cfg.refresh_token
    expires_in = int(data.get("expires_in") or 7200)
    if not access:
        raise RuntimeError(f"刷新结果无 access_token: {data}")
    apply_tokens_to_config(
        cfg,
        user_access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
    )
    persist_feishu_auth(
        user_access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
        config_path=config_path or str(default_config_path()),
    )
    return access


def run_oauth_login(
    cfg: FeishuConfig,
    *,
    config_path: Optional[str] = None,
    open_browser: bool = True,
    timeout_s: float = 180.0,
) -> Tuple[str, str]:
    """
    本地起回调服务 → 打开浏览器授权 → 换 token 并写入配置。
    返回 (user_access_token, 配置路径)。
    """
    app_id, app_secret, _, _ = _resolve_credentials(cfg)
    if not app_id or not app_secret:
        raise RuntimeError("请先在用户配置填写 feishu.app_id / app_secret")

    redirect = (cfg.redirect_uri or DEFAULT_REDIRECT).strip()
    parsed = urllib.parse.urlparse(redirect)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 18765)
    path = parsed.path or "/feishu/callback"

    holder: Dict[str, Any] = {}
    event = threading.Event()
    state = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != path:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(u.query)
            if qs.get("error"):
                holder["error"] = qs.get("error", ["unknown"])[0]
                body = "<h3>授权失败</h3><p>可关闭此页回到终端。</p>".encode("utf-8")
            elif not secrets.compare_digest((qs.get("state") or [""])[0], state):
                holder["error"] = "state 不匹配，疑似伪造回调"
                body = "<h3>回调校验失败</h3>".encode("utf-8")
            else:
                code = (qs.get("code") or [""])[0]
                if not code:
                    holder["error"] = "callback 无 code"
                    body = "<h3>未收到授权码</h3>".encode("utf-8")
                else:
                    holder["code"] = code
                    body = (
                        "<h3>授权成功</h3><p>可关闭此页，回到记忆沙箱终端。</p>".encode(
                            "utf-8"
                        )
                    )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            event.set()

    server = HTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    auth_url = build_authorize_url(cfg, state=state)
    print("请在飞书开放平台「安全设置 → 重定向 URL」添加：")
    print(f"  {redirect}")
    print("权限建议包含：offline_access、wiki/docx 读权限")
    print("正在打开浏览器授权…")
    print(auth_url)
    if open_browser:
        webbrowser.open(auth_url)

    if not event.wait(timeout_s):
        server.shutdown()
        raise RuntimeError(f"等待授权超时（>{timeout_s:.0f}s）")

    server.shutdown()
    if holder.get("error"):
        raise RuntimeError(f"授权失败: {holder['error']}")
    code = holder.get("code") or ""
    data = exchange_code_for_token(cfg, code)
    access = data.get("access_token") or data.get("user_access_token") or ""
    refresh = data.get("refresh_token") or ""
    expires_in = int(data.get("expires_in") or 7200)
    if not access:
        raise RuntimeError(f"换票结果无 access_token: {data}")
    if not refresh:
        # 没有 refresh_token 意味着 token 到期（默认 2 小时）后只能再次手动授权，
        # 静默通过会让人误以为已长期可用，所以这里必须显式提示。
        print(
            "警告：本次授权未下发 refresh_token，"
            f"token 将在约 {expires_in // 60} 分钟后失效且无法自动续期。\n"
            "  请在开放平台为应用开通 offline_access 权限，然后重新执行本登录流程。"
        )

    apply_tokens_to_config(
        cfg,
        user_access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
    )
    path_written = persist_feishu_auth(
        user_access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        config_path=config_path or str(app_support_dir() / "config.yaml"),
        enabled=True,
    )
    return access, path_written
