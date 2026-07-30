#!/usr/bin/env bash
# 把记忆沙箱 MCP 写入 Cursor 用户级配置（全局可用）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
SERVER="$ROOT/mcp_server.py"
TARGET="${HOME}/.cursor/mcp.json"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少虚拟环境，正在创建…"
  python3 -m venv "$ROOT/.venv"
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
  pip install -q -r "$ROOT/requirements.txt"
fi

mkdir -p "${HOME}/.cursor"

ENTRY=$(cat <<EOF
{
  "memory-sandbox": {
    "type": "stdio",
    "command": "${PYTHON}",
    "args": ["${SERVER}"],
    "env": { "PYTHONUNBUFFERED": "1" }
  }
}
EOF
)

if [[ -f "$TARGET" ]]; then
  "$PYTHON" - <<PY
import json
from pathlib import Path
target = Path("$TARGET")
data = json.loads(target.read_text(encoding="utf-8") or "{}")
servers = data.setdefault("mcpServers", {})
entry = json.loads('''$ENTRY''')
servers.update(entry)
target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已合并写入: {target}")
PY
else
  "$PYTHON" - <<PY
import json
from pathlib import Path
target = Path("$TARGET")
entry = json.loads('''$ENTRY''')
target.write_text(json.dumps({"mcpServers": entry}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已创建: {target}")
PY
fi

echo ""
echo "下一步："
echo "1. 重启 Cursor，或打开 Settings → MCP 刷新"
echo "2. 确认 memory-sandbox 显示为已连接"
echo "3. 在对话里试：用记忆沙箱查一下 agency 怎么启动"
