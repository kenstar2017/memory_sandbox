# 记忆沙箱（Memory Sandbox）

[English](./README.md) | 中文

模拟人类**感觉记忆 → 工作记忆 → 长时记忆**三级架构的本地分层内存系统。

**优先走沙箱检索/推理，沙箱无有效信息时才调用大模型**，适合日常切换开发环境后复用本地经验、降低 token 消耗。

## 架构

```
用户输入
  → 感觉记忆（TTL 瞬时缓冲、降噪）
  → 工作记忆（滑动窗口 + 规则/上下文推理）──命中──→ 直接输出
  → 长时记忆（向量库 + 程序性规则）────────命中──→ 直接输出
  → 大模型（仅沙箱无解）
  → 结果回写三层（记忆巩固）
```

| 层级 | 实现 | 时效 | 作用 |
|------|------|------|------|
| 感觉记忆 | 内存字典 | 2~5s TTL | 接收原文、过滤无效输入 |
| 工作记忆 | 滑动窗口（默认 7） | 会话内 / 空闲清空 | 短时上下文、本地规则推理 |
| 长时记忆 | JSON 向量库 + 规则表 | 持久化 | 历史问答、开发知识、模板 |

## 接入 Cursor（推荐）

通过 **MCP** 让 Cursor Agent 优先查本地记忆沙箱，再决定是否深入推理，减少重复问题 token。

### 1. 一键写入全局配置（任意项目可用）

```bash
cd memory_sandbox
./scripts/install_cursor_mcp.sh
```

会写入 `~/.cursor/mcp.json`。

本仓库也已自带项目级配置：`.cursor/mcp.json`。

### 2. 重启 Cursor

打开 **Settings → MCP**，确认 `memory-sandbox` 为已连接（绿点）。

### 3. 在对话里怎么用

直接说例如：

- 「用记忆沙箱查一下 agency 怎么启动」
- 「把这个结论记住：本地 mock 端口是 3001」

Agent 会调用这些工具：

| 工具 | 作用 |
|------|------|
| `memory_prepare` | **每轮首选**：拼接「记录到长期记忆」后检索；返回 `references`/`context_pack` 多条参考问答 |
| `memory_ask` | 原样检索（不自动拼后缀） |
| `memory_remember` | 固化可复用结论（可带 `tags`） |
| `memory_forget` | 主动遗忘 |
| `memory_status` | 查看记忆统计 |
| `memory_set_scene` | 切换场景（如 `dev`） |

项目规则 `.cursor/rules/memory-sandbox.mdc` 会引导 Agent：**先 `memory_prepare`**；把返回的 `references` / `context_pack` 当参考并结合当前仓库；改功能时不要因硬命中短路；纯事实复述且 `hit_local` 才可直接用 `answer`；结束前 `memory_remember`。

### 4. AI 记忆门禁：让「先查记忆、后落库」在所有项目里强制生效

上面那份规则是**项目级**的，只在本仓库生效。在别的项目里 Agent 既不知道要先检索、也不知道要落库：
一整轮排障或调研的结论会丢，或者凭空写文档而模板早就在记忆里。门禁用**用户级 hook**
（`~/.cursor/hooks.json` + `~/.cursor/hooks/`，全项目生效）在读写两侧各加一道：

四种安装方式，装的是同一套东西（逻辑都在 `core/cursor_hooks.py`）：

| 方式 | 怎么做 |
|------|--------|
| BloomBox 首次启动 | 弹窗询问一次，同意即装；拒绝了也不再打扰 |
| BloomBox 工具栏 | 点「AI 门禁」查看状态、启用、更新脚本或关闭 |
| 命令行 | `python3 main.py hooks-install` / `hooks-status` / `hooks-uninstall` |
| 脚本 | `./scripts/install_cursor_hooks.sh`（`--status` / `--uninstall`） |

安装是**合并**进 `~/.cursor/hooks.json`：只认领命令里含自己脚本名的条目，你自己配的 hook
一条都不会动，改写前还会把原文件备份成 `hooks.json.bak-<时间>`。可反复执行，不会产生重复条目；
脚本内容变了会被认成「待更新」，重新安装即可（按内容哈希判断，不靠版本号）。

| 脚本 | 事件 | 作用 |
|------|------|------|
| `memory-session-context.py` | `sessionStart` | 用 `additional_context` 把调用协议注入每个会话的初始系统上下文 |
| `memory-require-prepare.py` | `preToolUse` | 本轮没查过记忆就要动手改东西 → 拦一次，要求先 `memory_prepare` |
| `memory-mark.py` | `postToolUse` | 调过 prepare/ask 写 `.prepared`，调过 remember 写 `.remembered` |
| `memory-ensure-remember.py` | `stop` | 没落库就追问一轮；并按轮清掉读侧标记、清理 7 天前的过期状态 |

- 脚本源在仓库 `cursor_hooks/`，安装时拷到 `~/.cursor/hooks/`。脚本**只用标准库**，装完与本仓库
  彻底解耦：仓库删了、BloomBox 卸载了，hook 照样能跑（有单测守着这条约束）
- 标记按 `conversation_id` 存在 `~/.cursor/memory-sandbox-hook-state/`，每轮 `stop` 清一次 →
  「每轮都要查、每轮都要记」
- 读侧只拦 `Write` / `Delete` / `Task` 与飞书写操作；`Read` / `Grep` / `Shell` 和所有 `memory_*`
  工具照常放行
- 同一轮最多拦一次，MCP 挂了不会把整轮卡死；`stop` 配 `loop_limit: 1` 只追问一次
- 所有脚本失败放过（打印 `{}` / `permission: allow`、exit 0），不会挡住正常工作流

注意**不要**再往本仓库放项目级 `.cursor/hooks.json`——两份都在会重复执行。
`sessionStart` 注入要新开对话才生效；读侧门禁存盘即生效。

关于全局规则的两个坑：Cursor 不支持 `~/.cursor/rules/*.mdc`（静默忽略），User Rules 只能在
**Customize → Rules** 手填且不随 profile 导出；`beforeSubmitPrompt` 也注入不了上下文（只认
`continue` / `user_message`，多余字段静默丢弃）。上面的 `sessionStart` 注入就是用来替代手填的。

### 标签与类型（更好找）

- 写入时可带标签，如 `feishu`、`frontend`；问题里写 `#tag` 也可以
- 可标明类型：普通问答 / 命令 / 路径 / 环境变量 / 踩坑 / 决策
- 命中时会带上分数和原因（为什么会命中），方便核对或删除

### 省事与安全

- 粘贴终端/日志可先「提炼候选」，确认后再记住
- 写入时自动遮盖 token、密钥、私钥等敏感内容
- 网页左侧可按标签筛选，并编辑标签与类型

### 搜得更准、能分享

- 检索同时看语义向量、关键词和 BM25（Web 工具栏「检索设置」可调，每项有说明；也写入用户 `config.yaml`）
- 很久没用的条目会在检索时降权，也可一键归档
- 可导出「知识包」发给同事，对方合并导入即可（不含向量、已脱敏）

### 跟着代码变、和飞书联动

- `git-check`：对照 Git 变更，提示哪些记忆可能过时
- `review-suggest`：从近期提交提示可沉淀的协作习惯
- `feishu-bookmark`：把飞书文档拉成待确认记忆（需先登录飞书）
- `pack-list`：查看本机已导出的知识包

记忆数据与桌面 App 共用：

`~/Library/Application Support/MemorySandbox/`

## Mac 安装包（.dmg）

已提供可双击安装的本地 App：

```text
dist/MemorySandbox-0.1.1-mac.dmg
```

**安装：**

1. 双击打开 DMG  
2. 把 `MemorySandbox` 拖到 `Applications`  
3. 打开「记忆沙箱」后，会自动用浏览器打开本地界面（`http://127.0.0.1:8765`）

说明：macOS 26 上系统自带 tkinter/Tcl 会崩溃，因此 App 使用本地 Web UI（不依赖 tkinter）。

若提示无法验证开发者：右键 App → 打开 → 仍要打开。

记忆数据目录：`~/Library/Application Support/MemorySandbox/`

**重新打包：**

```bash
./scripts/build_dmg.sh
```

当前构建为 Apple Silicon（arm64）。

## 快速开始（源码）

```bash
cd memory_sandbox
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 本地 Web UI（推荐，兼容 macOS 26）
python3 app_web.py
# API-only（供桌面端 / 前后分离；不自动开浏览器）
# python3 app_web.py --api-only
# 旧版 tkinter GUI（macOS 26 系统 Python 可能崩溃）
# python3 app_gui.py

# CLI（与 MCP/Web 共用用户记忆目录）
./scripts/memory ask "revenue 怎么本地启动"
./scripts/memory ask --local "PK组件"          # 只查本地，不调沙箱 LLM
./scripts/memory prepare "revenue怎么启动"     # 拼接「记录到长期记忆」后查本地
./scripts/memory remember "问" "答" --scene dev --tag feishu
./scripts/memory list --layer long_term
./scripts/memory status
./scripts/memory backup
./scripts/memory                           # 交互模式

# 等价：
python3 main.py ask "如何启动本地前端"

# 写入开发种子 / 跑演示
python3 main.py seed
python3 examples/demo.py
```

可选：把 `scripts/memory` 链到 PATH：

```bash
ln -sf "$(pwd)/scripts/memory" /usr/local/bin/memory-sandbox
```

### 桌面端 BloomBox（React + Tauri，前后分离）

独立前端在 `desktop/`（应用名 **BloomBox**），通过 HTTP 调用本机 Python API（默认 `http://127.0.0.1:8765`）。**Tauri 启动时会自动执行** `python3 app_web.py --api-only`（端口已占用则复用；退出时只杀自己拉起的进程）。旧浏览器内嵌 UI 仍可用。

```bash
cd desktop
npm install
npm run tauri:dev   # 窗口 + 自动起 API（需 Rust / Xcode CLT）
# 仅浏览器联调前端时另开：python3 app_web.py --api-only
# npm run dev → http://localhost:5173
```

若 `npm run tauri:dev` 报 `failed to run 'cargo metadata' ... No such file or directory`，说明未装 Rust：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
# 若尚未安装：xcode-select --install
cd desktop && npm run tauri:dev
```

仍需本机 Python 3 与 `pip install -r requirements.txt`。可选：`BLOOMBOX_PYTHON`、`BLOOMBOX_API_ROOT`。详见 [`desktop/README.md`](desktop/README.md)。

环境变量：

| 变量 | 作用 |
|------|------|
| `MS_API_ONLY=1` | 等同 `--api-only` |
| `MS_CORS_ORIGINS` | 额外 CORS 来源（逗号分隔） |
| `VITE_API_BASE` | 前端 API 地址（默认 `http://127.0.0.1:8765`） |
| `BLOOMBOX_PYTHON` | BloomBox 拉起 API 时用的 Python 路径 |
| `BLOOMBOX_API_ROOT` | 含 `app_web.py` 的目录（开发默认仓库根） |

桌面端已对齐常用 Web 能力：聊天流式、标签筛选、Agent 模式、短时/长时/状态查看、检索设置、提炼候选、备份/清空/归档、导出知识包、优化问法、主题切换等。

### CLI 子命令

| 命令 | 说明 |
|------|------|
| `ask QUERY` | 提问（未命中可走沙箱 LLM）；加 `--local` 则只查本地 |
| `prepare QUERY` | 对齐 MCP：拼接后缀后只查本地 |
| `remember Q A` | 写入长时记忆 |
| `list` | 列出记忆（`--layer working\|long_term\|all`） |
| `status` | 各层统计 |
| `backup` / `restore` | 备份 / 恢复长时记忆 |
| `delete --id\|--question` | 删单条 |
| `forget [关键词]` | 按关键词遗忘或清空层（清空需 `--yes`） |
| `clear-long --yes` | 清空长时（可选 `--backup-first`） |
| `scene NAME` | 切换场景 |
| `agent-mode [ask\|plan\|agent]` | 查看/切换本地 Cursor Agent 模式 |
| `seed` / `reoptimize` | 种子记忆 / 刷新索引 |
| `interactive` | 交互模式 |

默认记忆目录：`~/Library/Application Support/MemorySandbox/memory`  
若要用项目内 `data/memory`，加 `--project-memory`。

### 交互模式内指令

| 指令 | 说明 |
|------|------|
| `记住：问题 => 答案` | 写入长时记忆 |
| `记住：某条知识` | 按知识片段固化 |
| `忘记刚才内容` | 清空工作/感觉记忆 |
| `忘记：关键词` | 按关键词清理各层 |
| `清空工作记忆` | 仅清空滑动窗口 |
| `切换场景：dev` | 情境依赖检索加权 |
| `切换Agent模式：ask\|plan\|agent` | 本地 LLM 只读/规划/可写 |
| `飞书登录` | 浏览器 OAuth 获取飞书读文档凭证 |
| `查看记忆状态` | 打印各层统计 |
| `帮助` | 规则引擎内置帮助 |

交互模式会在 stderr 显示着色阶段进度与旋转指示（检索 → 回退 LLM → Local Agent / Cloud）；答案在 stdout。遵循常见 CLI 约定：进度走 stderr、结果走 stdout；`NO_COLOR=1` 可关色；`--json` 不启用人机排版。

## 配置

见 `config.yaml`：

- `sensory.ttl`：感觉记忆过期秒数
- `working.chunk_size`：工作记忆窗口大小
- `long_term.similarity_threshold`：长时命中阈值（建议 0.65~0.75）
- `long_term.persist_dir`：持久化目录（默认 `data/memory`）
- `llm.provider`：`mock`（离线占位）| `cursor` | `openai_compatible`
- `llm.runtime`（仅 cursor）：`local`（本机 `agent` CLI，可读盘）| `cloud`（Cloud 无仓库，不能扫本机源码）
- `feishu.*`：飞书 wiki/docx 读写（见下方「飞书文档读写」）

## 飞书文档读写

对话中出现飞书 wiki/docx 链接时，沙箱可在回退 LLM 前 **自动拉取正文**；另有三个**写**能力
（改 wiki 标题、新建文档、改正文），见下方「写飞书文档」，**每次都需本人确认**。

- **不依赖** Cursor MCP、Trae MCP；**不改** `~/.cursor/mcp.json`
- 密钥只写本机用户配置，**勿提交 git**
- 代码：`core/feishu.py`、`core/feishu_oauth.py`；脚本：`scripts/feishu_login.py`、`scripts/configure_feishu.sh`

### 工作原理

```
用户输入含飞书链接
  → 本地三级记忆未命中
  → 解析 wiki/docx URL
  →（可选）用 refresh_token 续期 user_access_token
  → OpenAPI：wiki 节点 → docx raw_content
  → 正文注入 LLM 上下文 → 生成回答
```

| 点 | 说明 |
|----|------|
| 何时拉取 | 仅 Web / CLI 回退 LLM 前；MCP 的 `memory_prepare` **不会**拉飞书 |
| 不复用工作记忆旧答 | 含飞书链接的提问不直接复用上次失败/旧结论，便于重试 |
| 失败类答复 | 鉴权失败等不写入工作记忆 / 长时记忆 |
| 入库「问」 | 自动用 **文档标题 + 用户意图** 重写（末尾保留链接）；Web「补全答案」里「问」可编辑，也可点「优化问法」 |
| 不自动入库 | 飞书拉取成功后**不立刻写长时**，放入「待补全答」供你改问再确认；同文档 token / 指定 id 更新不会新开多条 |

### 配置位置

| 文件 | 用途 |
|------|------|
| 仓库 `config.yaml` | 字段模板，默认 `feishu.enabled: false` |
| **`~/Library/Application Support/MemorySandbox/config.yaml`** | **真正生效**（密钥写这里） |

环境变量（可选）：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_USER_ACCESS_TOKEN` / `FEISHU_API_BASE`。

### 接入步骤

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建应用，记下 App ID / App Secret。只读所需权限：`offline_access`、`docs:document.content:read`、`wiki:wiki:readonly` / `wiki:node:read`；要用写能力再按需加 `wiki:node:update`（改标题）、`docx:document:create` + `docx:document:readonly` + `docx:document:write_only`（新建文档、改正文）。**注意后台没有 `docx:document` 这一项**，虽然 API 文档统称它，实际只能勾这三项细分权限；另外权限分「应用身份 tenant_access_token」和「用户身份 user_access_token」两个 Tab，本项目用 user token，**必须在用户身份那一栏开通**。
2. 「安全设置 → 重定向 URL」添加（须与配置完全一致）：

```text
http://127.0.0.1:18765/feishu/callback
```

这是 **OAuth 登录回调**，不是 Web UI（`8765`），也不是事件订阅 Request URL。登录时本机临时监听 `18765` 接收授权码。

3. 写入用户配置：

```yaml
feishu:
  enabled: true
  app_id: "cli_xxx"
  app_secret: "xxx"
  redirect_uri: "http://127.0.0.1:18765/feishu/callback"
  api_base: "https://open.feishu.cn"
  # user_access_token / refresh_token 由 OAuth 写入，管理后台看不到明文
```

或：`FEISHU_APP_ID=... FEISHU_APP_SECRET=... ./scripts/configure_feishu.sh`

4. 浏览器授权（`user_access_token` 必须 OAuth 换取）：

```bash
python3 scripts/feishu_login.py
# 或对话发送：飞书登录
```

成功后写入 `user_access_token` + `refresh_token`；过期自动 refresh；长期失效再登录。

登录时会打印**实际授予**的权限，申请了但没批下来的会逐条列出——需审核权限（如
`docx:document:write_only`）常卡在这一步，早发现比等调用 API 报错好。

想拿到 `refresh_token`（否则每约 2 小时要重新登录）需要**两个**开关，缺一不可：

1. 「权限管理」开通 `offline_access`
2. 「安全设置」打开**「刷新 user_access_token」开关**（最易漏；只开权限不开开关照样不下发，
   刷新时会报 `20074`）

两处都改完要发布版本，再重跑登录。

5. 重启 Web / CLI，提问带飞书链接的问题即可（进度中应有「飞书文档：拉取…」）。

### 配置字段

| 字段 | 说明 |
|------|------|
| `enabled` | `true` 才启用 |
| `app_id` / `app_secret` | 开放平台应用凭证 |
| `redirect_uri` | 须与开放平台重定向 URL 一致 |
| `user_access_token` / `refresh_token` | OAuth 写入；后者用于续期 |
| `oauth_scope` | 申请的权限；新增权限后**必须重新登录**才生效（scope 固定在 token 里） |
| `doc_host` | 文档域名（如 `bytedance.larkoffice.com`），用于把新建文档的 `document_id` 拼成可点链接；留空只输出 id |
| `api_base` / `timeout` / `max_chars` | API 根、超时、注入截断长度 |

### 相关指令

| 指令 | 说明 |
|------|------|
| `飞书登录` / `飞书授权` / `登录飞书` | 浏览器 OAuth |
| `重试` / `再试一次` / `重新分析` | 跳过工作记忆复用 |
| `清空工作记忆` | 清掉短时旧结论 |

### 写飞书文档（每次需本人确认）

飞书文档多是团队共享内容，改动别人看得见且不易回滚，所以写能力做了**默认拒绝**的门禁：

- `core/feishu.py` 的写函数 `confirmed` 默认 `False`，未显式传 `True` 时**直接返回错误、不发任何请求**
- CLI 默认交互二次确认，答 `n` 即取消；`--yes` 仅供本人使用
- 约定见 `.cursor/rules/memory-sandbox.mdc`：AI 不得自行发起写操作，「上一轮批准过」不构成本次确认

改 wiki 节点标题：

```bash
python3 main.py feishu-set-title <飞书 wiki 链接> "<新标题>"
```

只支持 wiki 链接——docx 直链没有 `space_id` / `node_token`，拿不到就改不了。需权限 `wiki:node:update`。

新建文档（建在本人云空间，可带正文）：

```bash
python3 main.py feishu-create-doc "<标题>" --content-file notes.md
python3 main.py feishu-create-doc "<标题>" --folder <文件夹 token>   # 省略则建在根目录
```

需权限 `docx:document:create`（建文档）+ `docx:document:write_only`（写正文；只建空文档不需要它）。

改已有文档的正文（wiki 与 docx 链接都行；`--append` / `--replace` **必须显式选一个**）：

```bash
python3 main.py feishu-edit-body <链接> --append  --content-file notes.md   # 追加到末尾
python3 main.py feishu-edit-body <链接> --replace --content-file notes.md   # 删原正文再写
```

需 `docx:document:readonly`（数现有块）+ `docx:document:write_only`（增删块）。确认前会先**只读**拉一次目标文档，打印标题与现有块数，
`--replace` 还会明说「将删除原有 N 个块」——改错篇的代价比改错内容大得多。

- `--append` 不动原有内容，最安全
- `--replace` 会真的删掉原有块。飞书侧可用「历史版本」恢复，但别把它当撤销键
- 正文为空一律拒绝：不希望「文件恰好读空」变成把文档清空
- wiki 链接会先 `get_node` 换成 docx 的 `obj_token`，再按块操作

正文支持的 Markdown 子集：

- 块级：`#`~`######` 标题、`-`/`*` 无序列表、`1.` 有序列表、``` 代码块、`>` 引用、
 `---` 分割线、GFM 管道表格（`| a | b |` + `|---|---|`），其余非空行作普通段落
- 行内：`**粗体**`、`*斜体*`、`~~删除线~~`、`` `代码` ``、`[文字](链接)`
- 代码块与行内代码里的 Markdown 不再解析，`**` 和 `|` 保持字面量
- 表格必须走「创建嵌套块」接口（table → table_cell → 文本 三层），所以写入时
 表格单独发一次请求、平铺块按 50 一批发，二者按原文顺序交替追加
- 列宽按每列最长内容自动分配（中文按两格算，Markdown 标记不计宽），总宽约 800px 铺满正文区，
 每列保底 100px。不显式给 `column_width` 的话飞书会按一个偏小的默认值平分，
 长文件路径会被挤成一列一个字；列数过多时按保底宽度给，表格自身横向滚动

未支持的语法（会按纯文本写入）：图片、脚注、任务列表、嵌套列表缩进层级、单元格内换行。

这些能力同样暴露成 MCP 工具，所以在 Cursor 等外部 AI 工具里也能直接用，不必回到终端：

| 用途 | MCP 工具 | 对应 CLI |
|------|----------|----------|
| 读正文纯文本 | `memory_feishu_read` | — |
| 只读预览标题与块数（不含正文） | `memory_feishu_preview` | —（CLI 在确认前自动做） |
| 读评论 | `memory_feishu_list_comments` | `feishu-comments` |
| 加评论 / 回复评论 | `memory_feishu_comment` | `feishu-comment` |
| 新建文档 | `memory_feishu_create_doc` | `feishu-create-doc` |
| 改正文 | `memory_feishu_edit_body`（`mode=append`/`replace`） | `feishu-edit-body` |
| 改 wiki 标题 | `memory_feishu_set_title` | `feishu-set-title` |

三个读工具容易混，按需要的东西选：

- `memory_feishu_read` —— **要正文**（分析需求、照着写文档）。返回纯文本，默认单次最多 30000 字，
  超长时给 `next_offset`，用同一链接带 `offset` 续读，直到 `next_offset` 为 `null`
- `memory_feishu_preview` —— 只要「是哪一篇、多少块」，**不返回正文**，用于写操作前确认目标、省 token
- `memory_feishu_bookmark` —— 想把文档存成待确认记忆候选，正文会被截断到约 1200 字

三个写工具的 `confirmed` 都是 **required 且必须为 `true`**，漏传或传 `false` 都会直接报错、一个请求都不发出——
和 CLI 的交互确认是同一层门禁，AI 只有在你本轮明确同意后才可以传 `true`。改完 `mcp_server.py` 记得重启 MCP，
否则外部工具握手拿到的还是旧工具清单。

### 评论

```bash
python3 main.py feishu-comments <链接>                      # 列出全部评论（只读）
python3 main.py feishu-comment <链接> "这里建议补充埋点" --on "要评论的原文片段"   # 局部评论（写）
python3 main.py feishu-comment <链接> "整篇看下来还行"        # 全文评论，显示在文档底部（写）
python3 main.py feishu-comment <链接> "同意" --reply-to <comment_id>   # 回复某条评论
```

需 `docs:document.comment:read`（读）+ `docs:document.comment:create`（加/回复）。
聚合权限 `docs:doc`、`drive:drive` 也能调通，但范围大到整个云空间，不建议申请。

两种评论的区别，以及它们走的**不是同一个接口**：

| | 显示位置 | 接口 | 怎么发 |
|---|---|---|---|
| 局部评论（划词评论） | 正文旁边，带引用、可定位 | `POST /drive/v1/files/:token/new_comments`（v2 协议） | `--on` 或 `--block-id` |
| 全文评论 | 文档**最底部** | `POST /drive/v1/files/:token/comments`（v1） | 都不传 |

v1 的 `comments` 接口文档标题就叫「添加全文评论」，明说不支持局部评论；即使按响应体字段
去传 `is_whole` / `quote` 也会被**静默忽略**，永远建出全文评论。要锚定到某段文字，必须走
v2 的 `new_comments` 并传 `anchor.block_id`。

`--on` 传一小段连续且独特的原文即可，会先列出所有块按文字定位（跨粗体等样式边界也能匹配，
空白差异忽略）。**命中多个段落时直接报错并列出候选**，不猜——评论挂错位置比失败更糟；
这时换更独特的片段，或用 `--block-id` 直接指定。审阅场景建议逐个问题发局部评论，
而不是把多条意见塞进一条全文评论。

读没有这个限制：别人在客户端手动加的局部评论也能读出来，用返回里的 `is_whole` 与
`quote`（被选中的原文）区分。注意评论正文在 `replies` 数组里，不是顶层字段。

评论会通知文档协作者、别人立刻看得见，所以和改正文同一门禁：CLI 交互确认、MCP 的 `confirmed`
必须显式为 `true`。

### 写过的文档自动落库

新建、改正文、改标题只要在飞书侧真的生效，就会自动写一条长时记忆（MCP 与 CLI 都会），
不用再手动 `memory_remember`：

- 问法固定为 `《文档标题》飞书文档正文与写入记录 <链接>`，与读取型飞书记忆同构，
  所以「那篇客服工单的文档」既能命中读进来的正文，也能命中自己写过的改动
- 答案里记操作类型、时间、链接、`document_id`、写入/删除块数，外加正文大纲与约 600 字摘录
  （不塞全文，避免一条记忆过长拖垮检索）
- 打上 `feishu` / `docs` / `doc-write` 标签（评论是 `doc-comment`），`facts.path` 存链接
- 同一篇文档反复改会**更新同一条**，不会在库里堆成十几条
- 正文与评论是同一篇文档的两个**侧面**，各自一条、互不覆盖。去重链里有「同飞书 token 即同一条」
  这一档，所以光让问法不同拦不住覆盖；靠 `save_memory(dedup_facet=...)` 只在同侧面内合并
- MCP 返回里的 `remembered` 字段就是落库结果；CLI 会把它打在命令输出里

有两条边界值得知道：

| 情况 | 行为 |
|------|------|
| 未确认被拒、token 失效等飞书侧零改动 | **不落库**，免得把没发生的事记成改动史 |
| 文档建出来了但正文写失败 / 替换时删完写失败 | **落库**并标「未完成」，因为半成品和已删正文都需要有人记得去清理或恢复 |
| 落库本身失败（磁盘满等） | 命令仍按写成功返回，只提示落库失败——否则调用方以为没写成，重试会再建一篇重复文档 |

飞书接口本身的限制，实现里已处理，但值得知道：

| 限制 | 说明 |
|------|------|
| 创建接口只能给标题 | 正文得在建好后另调「创建块」接口写入，所以要编辑权限而非仅 create |
| 单次最多 50 个块 | 写入与删除都会自动分批，并按 3 次/秒限频加间隔 |
| 删除按索引区间 | `[start_index, end_index)` 左闭右开；分批时每轮都删最前面一批，因为删完后面的块会前移 |
| 新建时正文写一半失败 | 文档已经建出来了，错误信息里会带 `document_id` 供你去清理 |
| 替换时删完写失败 | 原正文已删，错误信息会提示去「历史版本」恢复 |

### 排障

| 现象 | 处理 |
|------|------|
| `99991668` Invalid access token | 再跑 `feishu_login.py` |
| 登录后没有 `refresh_token`（每 2 小时要重登） | `offline_access` 已授予也可能缺它：还要在「安全设置」打开「刷新 user_access_token」开关，发布后重登；刷新报 `20074` 同因 |
| 申请过的权限仍报权限不足 | 看登录输出的「未获授予」清单：需审核权限没批、或只开了「应用身份」栏漏了「用户身份」栏 |
| `131006` node permission denied | 个人文档靠 user token；或把文档授权给应用；写操作还需该节点的容器编辑权限 |
| `1770040` / `1770032` no folder permission | 新建文档时目标文件夹没有编辑权限，或未开通 `docx:document:write_only` |
| 缺 wiki scope | 开放平台开通 wiki 读权限 |
| 开了新权限仍报无权限 | scope 固定在 token 里，改完权限要重跑 `python3 scripts/feishu_login.py` |
| 授权页报 `20027`「当前应用权限不足：docx:document」 | 后台没有 `docx:document` 这个聚合权限，勾不到也就请求不到。已改为请求 `docx:document:create` / `:readonly` / `:write_only` 三项细分权限；旧配置里残留的 `docx:document` 会被 `_RETIRED_SCOPES` 自动剔除。同时确认：①「用户身份权限 user_access_token」Tab 也开通了（本项目用 user token，光开应用身份无效）②`:write_only` 属**需审核权限**，要等管理员批准 ③权限变更后要**创建版本并发布**才生效 |
| 回调失败 / 超时 | 重定向 URL 完全一致；`18765` 未被占用 |
| 只回旧答案 | 「清空工作记忆」或加「重试」；改代码后重启 Web |
| 改仓库 `config.yaml` 不生效 | 改 Application Support 用户配置 |

### 接入 Cursor 模型（记忆未命中时）

感觉 / 工作 / 长时均未命中时，CLI/Web 会回退 Cursor。

**默认 `runtime: local`**：调用本机 `agent` / `cursor-agent`，`--workspace` 指向 `llm.cwd`（空则用启动时的当前目录），默认 `--mode ask` 只读，可扫描/解释本地源码。

**`runtime: cloud`**：Cursor Cloud 无仓库 Agent，**看不到本机磁盘**。

推荐把密钥写到用户配置（不要提交仓库）：

`~/Library/Application Support/MemorySandbox/config.yaml`

```yaml
llm:
  enabled: true
  provider: cursor
  runtime: local          # local=读本机盘 | cloud=无仓库
  api_key: "crsr_..."
  model: ""               # 可空
  timeout: 600
  cwd: ""                 # 空 = 在哪个目录启动 CLI 就读哪个项目
  agent_mode: ask         # ask 只读；plan 规划；可写用 CLI/网页切到 agent
```

也可用环境变量：`CURSOR_API_KEY` / `CURSOR_MODEL` / `CURSOR_CWD`。

需已安装 Cursor Agent CLI（`agent` 在 PATH）。找不到时会明确报错，不会静默改走 Cloud。

**切换 Agent 模式（Ask / Plan / Agent）**

| 入口 | 用法 |
|------|------|
| CLI | `memory-sandbox agent-mode` 查看；`memory-sandbox agent-mode agent` 切换并持久化 |
| CLI 交互 | `切换Agent模式：ask` / `plan` / `agent` |
| CLI 单次 | `memory-sandbox ask --agent-mode agent "…"`（本轮不落盘） |
| Web | 工具栏「Agent 模式」下拉框 |

`agent` = 全工具可写（会带 `--force`，慎用）；`ask`/`plan` = 只读。设置写入用户配置 `~/Library/Application Support/MemorySandbox/config.yaml`。

沙箱回退的 Local/Cloud Agent **禁止 `git push` / 开 PR**；需要推远程时请你在本机终端自行执行。

### 接入 OpenAI 兼容网关

```yaml
llm:
  enabled: true
  provider: openai_compatible
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
  model: "gpt-4o-mini"
```

环境变量：`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`。

## 代码入口

```python
from core import MemorySandbox

sb = MemorySandbox()                 # 读取 config.yaml
result = sb.chat("本地 mock 端口是多少")
print(result.answer, result.source)  # source: working | long_term | llm | ...
```

## 目录结构

```
memory_sandbox/
├── config.yaml          # 阈值与 LLM 配置
├── main.py              # CLI
├── app_web.py           # Web UI + /api/*（支持 --api-only）
├── desktop/             # React + Vite + Tauri 桌面前端
├── core/
│   ├── sensory.py       # 感觉记忆
│   ├── working.py       # 工作记忆
│   ├── long_term.py     # 长时记忆
│   ├── embedding.py     # 本地哈希向量（无模型下载）
│   ├── rules.py         # 轻量规则引擎
│   ├── llm.py           # 大模型适配
│   └── sandbox.py       # 主编排 chat()
├── examples/demo.py
└── data/memory/         # 运行后生成的持久化数据
```

## 设计取舍（本地落地）

1. **Embedding**：默认本地特征哈希向量，免下载模型，保证同库可复现匹配。
2. **向量库**：默认 JSON 持久化，零外部服务；数据量变大时可再换 Chroma/FAISS。
3. **LLM**：可插拔；未配置时用 MockLLM，保证离线链路完整可跑。
4. **记忆强化**：命中提升 `weight`；高频短问答可沉淀进工作记忆 FAQ。
