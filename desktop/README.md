# BloomBox（React + Tauri）

记忆沙箱的 Mac 桌面前端：通过 HTTP 调用本机 Python `/api/*`。

## 开发

BloomBox（`tauri:dev` / 打包后的 `.app`）会**自动**在本机拉起 `python3 app_web.py --api-only`（默认 `127.0.0.1:8765`）。若该端口已有健康服务则复用；退出应用时仅杀掉自己拉起的进程。

复用前会做两道新旧判定，因为「后端是常驻进程、改了代码不会热更」这件事很容易被忽略：

1. `UI_FEATURES`：随包 `app_web.py` 声明的能力，跑着的实例缺任何一项就换掉（认出「加了新接口的旧进程」）
2. `code_stamp`：`app_web.py` + `core/*.py` 的内容指纹。改动只落在 `core/` 时特性名不会变，旧进程照样 200，只有指纹能认出来

指纹算法在 `app_web.py::compute_code_stamp` 与 `src-tauri/src/api_server.rs::expected_code_stamp` 各实现一份，必须逐字节一致，两边各有一个断言同一样例指纹（`ca0f047bc734`）的测试守着。任一侧算不出就当「无意见」不重启，避免误杀。开发态 `resolve_api_root` 优先仓库根，手动起的调试后端不会被换掉。

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

## 新记忆提醒

`src/hooks/useMemoryWatch.ts` 每 5 秒问一次 `GET /api/long_term_revision`（只回 `mtime:size`，不回内容），
变了就刷新列表并标出新增：侧栏「N 条新」胶囊 + 高亮圆点，对话区留一条提示。这样别的项目里 agent
通过 MCP 写入的记忆也能看见。窗口不可见时停止轮询；首个标记只作基线，否则一进来全量都算新。

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
| `npm run sync-api` | 把 `app_web.py` / `feishu_bot.py` / `core/` / `cursor_hooks/` 等同步到 `src-tauri/resources/api` |
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

### `tauri:build` 里那个 `CI=true` 别删

打 DMG 的最后一步是卸载临时挂载卷，而卷会被前一步「Finder 美化」（`bundle_dmg.sh` 调
AppleScript 摆图标位置）占住，卸载报「资源忙」，整个打包就以
`error running bundle_dmg.sh` 失败——`.app` 其实已经打好了，只有 DMG 这步挂。

脚本自带的卸载重试救不了：它只在 `hdiutil` 返回 16（EBUSY）时才重试，而这台 macOS 上
「资源忙」返回的不是 16，于是一次就 `exit`。

`CI=true` 会让 Tauri 给 `bundle_dmg.sh` 传 `--skip-jenkins`，跳过那段 AppleScript，
Finder 不再碰这个卷，卸载就正常了（代价：DMG 里没有自定义图标位置，功能不受影响）。
想反过来强行保留美化可设 `TAURI_BUNDLER_DMG_IGNORE_CI=1`，但在本机会复现上面的失败。

失败过的话会**留下挂载卷**，堆着会影响下次打包，先清掉再重试：

```bash
for v in /Volumes/dmg.*; do hdiutil detach "$v"; done
```

## AI 记忆门禁（装机后可用）

别人装了这个包，首次启动会被问一次是否开启「AI 记忆门禁」（给 Cursor 装 hook，让所有项目里的
AI 动手前先查记忆、结束前落库）；同意即装，拒绝后可在工具栏「AI 门禁」里随时开关。

- 前端：`src/components/CursorHooksModal.tsx`，走 `/api/cursor_hooks/{status,install,uninstall}`
- 后端：`core/cursor_hooks.py`（合并写入 `~/.cursor/hooks.json`，不动用户已有 hook）
- 脚本源：仓库 `cursor_hooks/`，由 `npm run sync-api` 打进 `resources/api/cursor_hooks/`
  ——**漏了这步装机后会提示「安装包里缺少 hook 脚本」**

说明：打包时会把 API 源码打进 `Resources`；启动时自动执行 `--api-only`。请确保系统有可用的 `python3` 与依赖。

## 飞书机器人（顶栏「飞书机器人」）

常驻的长连接进程原先只能自己开终端跑，这里把它托管起来：状态、日志尾巴、启动/重启/停止。

- 入口在顶栏（主题、「记忆」同一排），不在下面那排工具栏：它是「现在在不在跑」的
  常驻指示，按钮上带一颗点，`App.tsx` 每 20 秒轮询一次（窗口不可见时跳过）
- 前端：`src/components/FeishuBotModal.tsx`，走 `/api/feishu_bot/{status,start,stop,restart}`
- 后端：`core/bot_process.py`（pidfile + 命令行比对判活，日志写 Application Support）
- 脚本源：仓库根的 `feishu_bot.py`，由 `npm run sync-api` 拷进 `resources/api/`
  ——**漏了这步装机后点启动只会说「找不到 feishu_bot.py」**
- 机器人自成会话，**退出 BloomBox 不会把它带走**；Rust 那边只管 `app_web.py`，
  别把机器人也塞进 `ApiServerState`，那样每次关窗口飞书就掉线

未签名时，首次打开若被拦截：系统设置 → 隐私与安全性 → 仍要打开；或：

```bash
xattr -cr /Applications/BloomBox.app
```
