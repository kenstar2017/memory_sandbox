#!/usr/bin/env bash
# 把 Python API 源码同步到 src-tauri/resources/api，供打包进 .app
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/desktop/src-tauri/resources/api"
rm -rf "$DEST"
mkdir -p "$DEST/core"
cp "$ROOT/app_web.py" "$DEST/"
cp "$ROOT/config.yaml" "$DEST/"
cp "$ROOT/requirements.txt" "$DEST/" 2>/dev/null || true
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '*.pyo' \
  "$ROOT/core/" "$DEST/core/"
printf '%s\n' \
  'BloomBox bundled API sources.' \
  'Started by the app as: python3 app_web.py --api-only' \
  > "$DEST/README.txt"
echo "Synced API resources → $DEST"
