#!/usr/bin/env bash
# 写入飞书应用凭证到记忆沙箱用户配置（不改 Cursor）。
# user_access_token 请用 OAuth 获取：python3 scripts/feishu_login.py（管理后台无明文）。
# 用法：
#   FEISHU_APP_ID=... FEISHU_APP_SECRET=... ./scripts/configure_feishu.sh
#   ./scripts/configure_feishu.sh --from-trae   # 只拷贝本机 Trae 文件里的 app 凭证
#   ./scripts/configure_feishu.sh --prompt
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-}"
TRAE_MCP="${HOME}/Library/Application Support/Trae CN/User/mcp.json"
TARGET="${HOME}/Library/Application Support/MemorySandbox/config.yaml"

APP_ID="${FEISHU_APP_ID:-}"
APP_SECRET="${FEISHU_APP_SECRET:-}"
USER_TOKEN="${FEISHU_USER_ACCESS_TOKEN:-}"
API_BASE="${FEISHU_API_BASE:-https://open.feishu.cn}"

if [[ "$MODE" == "--from-trae" ]]; then
  if [[ ! -f "$TRAE_MCP" ]]; then
    echo "未找到本地 Trae MCP: $TRAE_MCP" >&2
    exit 1
  fi
  eval "$(python3 - <<'PY'
import json
from pathlib import Path
p = Path.home() / "Library/Application Support/Trae CN/User/mcp.json"
data = json.loads(p.read_text(encoding="utf-8") or "{}")
servers = data.get("mcpServers") or data
entry = servers.get("@mcp_hub/lark-doc") or {}
env = entry.get("env") or {}
def sh(k, v):
    v = (v or "").replace("'", "'\"'\"'")
    print(f"{k}='{v}'")
sh("APP_ID", env.get("APP_ID", ""))
sh("APP_SECRET", env.get("APP_SECRET", ""))
sh("USER_TOKEN", env.get("USER_ACCESS_TOKEN", ""))
PY
)"
  echo "已从本机 Trae mcp.json 读取 @mcp_hub/lark-doc 凭证（未改 Trae）"
elif [[ "$MODE" == "--prompt" ]]; then
  read -r -p "FEISHU_APP_ID: " APP_ID
  read -r -p "FEISHU_APP_SECRET: " APP_SECRET
  read -r -p "FEISHU_USER_ACCESS_TOKEN: " USER_TOKEN
  read -r -p "API_BASE [${API_BASE}]: " _b
  API_BASE="${_b:-$API_BASE}"
fi

if [[ -z "$APP_ID" || -z "$APP_SECRET" ]]; then
  echo "缺少 APP_ID/APP_SECRET。请设置环境变量，或使用 --from-trae / --prompt" >&2
  exit 1
fi
if [[ -z "$USER_TOKEN" ]]; then
  echo "警告：未设置 USER_ACCESS_TOKEN，仅能读取已授权给应用的文档。" >&2
fi

mkdir -p "$(dirname "$TARGET")"
python3 - <<PY
import yaml
from pathlib import Path

target = Path(r"""$TARGET""")
raw = {}
if target.is_file():
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

feishu = dict(raw.get("feishu") or {})
feishu.update({
    "enabled": True,
    "app_id": """$APP_ID""",
    "app_secret": """$APP_SECRET""",
    "user_access_token": """$USER_TOKEN""",
    "api_base": """$API_BASE""",
    "timeout": float(feishu.get("timeout") or 30),
    "max_chars": int(feishu.get("max_chars") or 80000),
})
raw["feishu"] = feishu
target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"已写入: {target}")
print("  feishu.enabled=true")
print("  app_id / secret / user_access_token 已设置（值未打印）")
print("下一步（必做）：python3 scripts/feishu_login.py")
print("  或在沙箱对话发送「飞书登录」——浏览器授权获取 user_access_token（非管理后台明文）。")
PY
