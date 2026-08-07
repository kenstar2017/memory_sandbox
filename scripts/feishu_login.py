#!/usr/bin/env python3
"""飞书用户授权登录：浏览器 OAuth 动态获取 user_access_token / refresh_token。

user_access_token 不能在飞书管理后台查看明文，必须走授权码换票。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.feishu_oauth import DEFAULT_REDIRECT, run_oauth_login
from core.paths import app_support_dir, default_config_path


def main() -> int:
    cfg_path = str(default_config_path())
    # 登录结果写入用户目录
    user_cfg = str(app_support_dir() / "config.yaml")
    cfg = load_config(cfg_path)
    feishu = cfg.feishu
    if not feishu.app_id or not feishu.app_secret:
        print(
            "请先在用户配置填写 app_id / app_secret：\n"
            f"  {user_cfg}\n"
            "或：FEISHU_APP_ID=... FEISHU_APP_SECRET=... ./scripts/configure_feishu.sh",
            file=sys.stderr,
        )
        return 1

    redirect = (feishu.redirect_uri or DEFAULT_REDIRECT).strip()
    print("=== 记忆沙箱 · 飞书 OAuth 登录 ===")
    print(f"app_id: {feishu.app_id}")
    print(f"redirect_uri: {redirect}")
    print("（请在开放平台「安全设置 → 重定向 URL」添加上述地址）")
    try:
        _, written = run_oauth_login(feishu, config_path=user_cfg, open_browser=True)
    except Exception as e:
        print(f"登录失败: {e}", file=sys.stderr)
        return 2

    print(f"已写入: {written}")
    # 没拿到 refresh_token 时不能谎报已保存，否则用户以为能自动续期
    saved = load_config(written).feishu
    if saved.refresh_token:
        print("已保存 user_access_token + refresh_token（明文不打印）。")
        print("之后 token 过期会自动用 refresh_token 续期；长期失效再跑本脚本。")
    else:
        print("已保存 user_access_token（明文不打印）；本次没有 refresh_token。")
        print("token 到期后需重跑本脚本，除非按上面三步开通自动续期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
