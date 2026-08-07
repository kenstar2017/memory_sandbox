#!/usr/bin/env bash
# 把记忆沙箱的 Cursor hook 门禁装到 ~/.cursor（合并进已有配置，不覆盖）。
# 逻辑都在 core/cursor_hooks.py，这里只是给不想记命令的人一个入口。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON" ]]; then
  echo "找不到 python3，请先安装 Python 3" >&2
  exit 1
fi

ACTION="install"
case "${1:-}" in
  --uninstall) ACTION="uninstall" ;;
  --status) ACTION="status" ;;
  --help|-h)
    cat <<'EOF'
用法: install_cursor_hooks.sh [--status | --uninstall]

  （无参数）  安装/更新 hook 门禁到 ~/.cursor
  --status    查看安装状态
  --uninstall 移除（只删自己的条目，保留你其它 hook）

装上之后：AI 动手改东西前会被要求先查记忆，结束时没落库会被追问一轮。
sessionStart 注入要新开对话才生效。
EOF
    exit 0
    ;;
  "") ;;
  *)
    echo "未知参数：$1（用 --help 看用法）" >&2
    exit 2
    ;;
esac

cd "$ROOT"
exec "$PYTHON" main.py "hooks-${ACTION}"
