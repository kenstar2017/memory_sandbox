# BloomBox（React + Tauri）

记忆沙箱的 Mac 桌面前端：通过 HTTP 调用本机 Python `/api/*`。

## 开发

BloomBox（`tauri:dev` / 打包后的 `.app`）会**自动**在本机拉起 `python3 app_web.py --api-only`（默认 `127.0.0.1:8765`）。若该端口已有健康服务则复用；退出应用时仅杀掉自己拉起的进程。

```bash
cd desktop
npm install
npm run tauri:dev   # 原生窗口 + 自动起 API（需 Rust，见下）
# 仅浏览器联调前端时仍需另开 API：
#   仓库根：python3 app_web.py --api-only
npm run dev         # → http://localhost:5173
```

默认 API：`http://127.0.0.1:8765`（可用 `VITE_API_BASE` 覆盖）。

环境变量（可选）：

| 变量 | 说明 |
|------|------|
| `BLOOMBOX_PYTHON` | Python 可执行文件路径 |
| `BLOOMBOX_API_ROOT` | 含 `app_web.py` 的目录（开发默认同仓库根；发布包用内置 `resources/api`） |

仍需本机已装 Python 3 与 `pip install -r requirements.txt`（尚未内嵌 PyInstaller sidecar）。

### 若报错 `failed to run 'cargo metadata' ... No such file or directory`

本机还没有 Rust/`cargo`。Tauri 编译原生壳必须装：

```bash
# 1) 安装 Rust（官方 rustup）
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

# 2) 确认
cargo --version
rustc --version

# 3) macOS 还需 Xcode 命令行工具（若尚未安装）
xcode-select --install

# 4) 再启桌面
cd desktop && npm run tauri:dev
```

装好前可用 `npm run dev` 在浏览器验证前后端联调，功能与 MVP 一致。

### Dock 仍是默认 Tauri 图标？

`npm run tauri:dev` 跑的是调试二进制（不是完整 `.app`）。改图标后需：

1. 完全退出旧进程（Dock 上点退出）
2. 再执行 `npm run tauri:dev`（应用名 BloomBox）
3. 若仍缓存旧图：`killall Dock`（Dock 会自动重启）

正式 Dock 图标以打包为准：`npm run tauri:build` 后打开生成的 `BloomBox.app`。

## 脚本

| 命令 | 说明 |
|------|------|
| `npm run dev` | Vite 开发服（5173） |
| `npm run tauri:dev` | Tauri 窗口 + Vite |
| `npm run build` | 构建前端到 `dist/` |
| `npm run sync-api` | 把 `app_web.py` / `core/` / `cursor_hooks/` 等同步到 `src-tauri/resources/api` |
| `npm run tauri:build` | sync-api + 打包可安装桌面应用 |

## 生成可安装应用（macOS）

前置：已装 Rust（`cargo -V`）、Xcode CLT（`xcode-select -p`）、本目录 `npm install`。

```bash
cd desktop
npm run tauri:build
```

产物一般在：

```text
src-tauri/target/release/bundle/macos/BloomBox.app
src-tauri/target/release/bundle/dmg/BloomBox_0.1.0_*.dmg
```

- 直接用：把 `BloomBox.app` 拖到「应用程序」
- 或打开 `.dmg` 再拖进去安装

## AI 记忆门禁（装机后可用）

别人装了这个包，首次启动会被问一次是否开启「AI 记忆门禁」（给 Cursor 装 hook，让所有项目里的
AI 动手前先查记忆、结束前落库）；同意即装，拒绝后可在工具栏「AI 门禁」里随时开关。

- 前端：`src/components/CursorHooksModal.tsx`，走 `/api/cursor_hooks/{status,install,uninstall}`
- 后端：`core/cursor_hooks.py`（合并写入 `~/.cursor/hooks.json`，不动用户已有 hook）
- 脚本源：仓库 `cursor_hooks/`，由 `npm run sync-api` 打进 `resources/api/cursor_hooks/`
  ——**漏了这步装机后会提示「安装包里缺少 hook 脚本」**

说明：打包时会把 API 源码打进 `Resources`；启动时自动执行 `--api-only`。请确保系统有可用的 `python3` 与依赖。

未签名时，首次打开若被拦截：系统设置 → 隐私与安全性 → 仍要打开；或：

```bash
xattr -cr /Applications/BloomBox.app
```
