#!/usr/bin/env bash
# 把 Python API 源码同步到 src-tauri/resources/api，供打包进 .app
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/desktop/src-tauri/resources/api"
rm -rf "$DEST"
mkdir -p "$DEST/core"
cp "$ROOT/app_web.py" "$DEST/"
# 飞书机器人常驻进程：应用里「飞书机器人」按钮起的就是它（core/bot_process.py 从
# 这个目录找 feishu_bot.py），漏了装机后按启动只会提示「找不到 feishu_bot.py」
cp "$ROOT/feishu_bot.py" "$DEST/"
cp "$ROOT/config.yaml" "$DEST/"
cp "$ROOT/requirements.txt" "$DEST/" 2>/dev/null || true
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '*.pyo' \
  "$ROOT/core/" "$DEST/core/"
# Cursor hook 门禁的源脚本：core/cursor_hooks.py 从这里拷到用户的 ~/.cursor/hooks/，
# 少了它装机后就只有空壳（source_dir() 会找不到而报「无法安装」）
mkdir -p "$DEST/cursor_hooks"
rsync -a --exclude '__pycache__' --exclude '*.pyc' \
  "$ROOT/cursor_hooks/" "$DEST/cursor_hooks/"
printf '%s\n' \
  'BloomBox bundled API sources.' \
  'Started by the app as: python3 app_web.py --api-only' \
  'feishu_bot.py is launched on demand from the app (飞书机器人 button).' \
  'cursor_hooks/ holds the Cursor hook scripts installed into ~/.cursor/hooks/.' \
  > "$DEST/README.txt"
echo "Synced API resources → $DEST"
