#!/usr/bin/env bash
# 构建 macOS .app 并打包为可安装的 .dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="MemorySandbox"
VERSION="0.1.2"
DIST_DIR="$ROOT/dist"
BUILD_DIR="$ROOT/build"
PKG_DIR="$ROOT/packaging"
DMG_STAGING="$DIST_DIR/dmg_staging"
DMG_PATH="$DIST_DIR/${APP_NAME}-${VERSION}-mac.dmg"
ICONSET="$PKG_DIR/MemorySandbox.iconset"
ICNS="$PKG_DIR/MemorySandbox.icns"
ICON_SRC="$PKG_DIR/app_icon_1024.png"

echo "==> [1/5] 准备虚拟环境与依赖"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install -q -r "$ROOT/requirements.txt"
pip install -q "pyinstaller>=6.0"

echo "==> [2/5] 生成 .icns 图标"
if [[ -f "$ICON_SRC" ]]; then
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  sips -z 16 16     "$ICON_SRC" --out "$ICONSET/icon_16x16.png" >/dev/null
  sips -z 32 32     "$ICON_SRC" --out "$ICONSET/diana.k@example.org" >/dev/null
  sips -z 32 32     "$ICON_SRC" --out "$ICONSET/icon_32x32.png" >/dev/null
  sips -z 64 64     "$ICON_SRC" --out "$ICONSET/wendy.h@example.net" >/dev/null
  sips -z 128 128   "$ICON_SRC" --out "$ICONSET/icon_128x128.png" >/dev/null
  sips -z 256 256   "$ICON_SRC" --out "$ICONSET/wendy.h@example.net" >/dev/null
  sips -z 256 256   "$ICON_SRC" --out "$ICONSET/icon_256x256.png" >/dev/null
  sips -z 512 512   "$ICON_SRC" --out "$ICONSET/wendy.h@example.net" >/dev/null
  sips -z 512 512   "$ICON_SRC" --out "$ICONSET/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$ICON_SRC" --out "$ICONSET/walt.e@example.net" >/dev/null
  iconutil -c icns "$ICONSET" -o "$ICNS"
  rm -rf "$ICONSET"
  echo "    icns -> $ICNS"
else
  echo "    警告: 未找到 $ICON_SRC，将使用默认图标"
fi

echo "==> [3/5] PyInstaller 打包 .app"
rm -rf "$BUILD_DIR" "$DIST_DIR/$APP_NAME" "$DIST_DIR/$APP_NAME.app"
pyinstaller --noconfirm --clean "$PKG_DIR/memory_sandbox.spec"

APP_PATH="$DIST_DIR/$APP_NAME.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "错误: 未生成 $APP_PATH"
  exit 1
fi

# 清除隔离属性 + ad-hoc 签名（本机可运行；未公证仍可能需右键打开）
xattr -cr "$APP_PATH" || true
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep "$APP_PATH"

echo "==> [4/5] 组装 DMG 内容"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "$APP_PATH" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

# 简易说明
cat > "$DMG_STAGING/安装说明.txt" <<'EOF'
记忆沙箱 Memory Sandbox
========================

安装：
1. 将「MemorySandbox」拖到「Applications」文件夹
2. 打开「启动台」或「应用程序」中的「记忆沙箱」
3. 会自动用浏览器打开本地界面（http://127.0.0.1:8765）

说明：
- 本版本使用本地 Web UI，避免 macOS 26 系统 tkinter/Tcl 崩溃
- 关闭浏览器不会退出 App；可从程序坞右键退出「记忆沙箱」

若提示「无法验证开发者」：
- 右键 App → 打开 → 仍要打开

数据目录：
~/Library/Application Support/MemorySandbox/

常用指令：
记住：问题 => 答案
忘记刚才内容
查看记忆状态
切换场景：dev
EOF

echo "==> [5/5] 生成 DMG: $DMG_PATH"
rm -f "$DMG_PATH"
hdiutil create \
  -volname "MemorySandbox" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

# 清理 staging（保留 .app 与 .dmg）
rm -rf "$DMG_STAGING"

echo ""
echo "构建完成:"
echo "  App : $APP_PATH"
echo "  DMG : $DMG_PATH"
ls -lh "$DMG_PATH"
