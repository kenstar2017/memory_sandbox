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
| `memory_update` | **修正过时记忆**：按 id 原地改写（取值变了、规范改了、结论被推翻） |
| `memory_delete` | 整条已失效时删掉 |
| `memory_forget` | 主动遗忘 |
| `memory_status` | 查看记忆统计 |
| `memory_set_scene` | 切换场景（如 `dev`） |

项目规则 `.cursor/rules/memory-sandbox.mdc` 会引导 Agent：**先 `memory_prepare`**；把返回的 `references` / `context_pack` 当参考并结合当前仓库；改功能时不要因硬命中短路；纯事实复述且 `hit_local` 才可直接用 `answer`；结束前 `memory_remember`。

**维护记忆和写入记忆一样重要**。知识库最大的失效方式不是缺内容，而是留着一条早已被推翻的旧结论——它照样会被检索命中，还长得像权威答案。所以每条召回都带 `id=`（`references` 里有字段，`context_pack` 文本里也印出来），Agent 发现某条与现状矛盾时可以直接 `memory_update(memory_id=..., answer=...)` 改掉；这条职责也写进了门禁（见下）。

`memory_update` 的两个设计取舍：**定位不到原条目就报错，绝不新建**（"更新"写成新增会让新旧两种说法同时留在库里，检索时打架，比不更新更糟）；**省略的字段沿用原值**，只想改结论就只传 `answer`，不必把问法和标签重报一遍（库里的问法常是精心调过的，反复重报会越改越偏）。

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
| `memory-prefetch.py` | `beforeSubmitPrompt` | **你一按发送就用原话检索一次**，把召回结果存好待投递；并按轮重置读侧标记 |
| `memory-session-context.py` | `sessionStart` | 用 `additional_context` 把调用协议注入会话初始上下文（尽力而为，见下） |
| `memory-require-prepare.py` | `preToolUse` | 有预取包 → 拦本轮第一个工具调用，把召回内容用 `agent_message` **直接交给模型**；没有 → 只拦「动手」并要求先 `memory_prepare` |
| `memory-mark.py` | `postToolUse` | 调过 prepare/ask 写 `.prepared`，调过 remember 写 `.remembered` |
| `memory-ensure-remember.py` | `stop` | 没落库就追问一轮；并按轮清掉读侧标记、清理 7 天前的过期状态 |

**为什么要在提问前就检索**：等 Agent 自己想起来调 `memory_prepare` 有两个问题——它可能不调，
调了也是用它转述的检索词。预取用的是**你的原话**，而且在它开工之前就取完了。

检索走本机 `POST /api/prepare`（只做软召回：不拼「记录到长期记忆」、不造待补全、不 reinforce，
否则每条消息都会污染权重与命中数）。这个接口由 BloomBox / `app_web.py --api-only` 提供，
**没在跑就自动降级**回「要求 Agent 自己查一次」，不会报错也不会卡住发送（超时 1.5 秒）。

投递为什么走 `preToolUse` 的 `agent_message` 而不是直接注入 prompt：

- `beforeSubmitPrompt` **改不了 prompt**。官方明确回复它只认 `continue` / `user_message`，
  `updated_input` 之类一概不支持；而校验器**不拒绝未知字段**，所以返回 `additional_context`
  会静默失效——Hooks 日志里显示「Merged 1 valid response(s)」，模型却什么也没收到
- `sessionStart` 的 `additional_context` **也不可靠**（此前本文档说它是可用注入点，是过时结论）：
  它经 composer handle 写入，而 `sessionStart` 是异步的，常在 handle 建好前就跑完，于是被静默丢弃；
  最容易丢的恰好是新会话第一条消息
- `postToolUse` 的 `additional_context` 官方承认消费端未完成，到不了模型
- `preToolUse` 拒绝时的 `agent_message` 是**有文档、且实测能把文本送到模型**的通道，所以拿它当投递口

因此读侧的拦截规则是「有货才拦」：

- 预取到了相关记忆 → 拦本轮第一个工具调用（含 `Read` / `Grep`），把知识交出去，本轮不再拦。
  交出去的同时要求 Agent **顺手维护**：某条与现状矛盾就用括号里的 `id=` 调 `memory_update` 改掉，
  整条失效就 `memory_delete`；只在回答里说一句「这条过时了」不算处理
- 记忆里没有相关内容 → 预取阶段直接标记本轮通过，**一次都不拦**
- 后端没跑（取不到）→ 退回旧规则：只拦 `Write` / `Delete` / `Task` 与飞书写操作
- 所有 `memory_*` 工具永远放行，否则「先查记忆」这条路本身就被堵死了

其余约束：

- 脚本源在仓库 `cursor_hooks/`，安装时拷到 `~/.cursor/hooks/`。脚本**只用标准库**，装完与本仓库
  彻底解耦：仓库删了、BloomBox 卸载了，hook 照样能跑（有单测守着这条约束）
- 标记按 `conversation_id` 存在 `~/.cursor/memory-sandbox-hook-state/`，每轮开头（`beforeSubmitPrompt`）
  清一次、`stop` 兜底再清一次 → 「每轮都要查、每轮都要记」
- 同一轮最多拦一次，MCP 挂了不会把整轮卡死；`stop` 配 `loop_limit: 1` 只追问一次
- 所有脚本失败放过（打印 `{"continue": true}` / `{}` / `permission: allow`、exit 0），不会挡住正常工作流
- 子 Agent 跑在自己新的 `conversation_id` 下，拿不到父会话的预取包，所以它会自己查一次
- **`MEMORY_SANDBOX_NESTED=1` 时五个脚本全部空转**。机器人把本机 agent CLI 当模型用
  （`core/llm.py` 的 `CursorLocalAgentLLM`，带 `--approve-mcps`），那个嵌套进程也有记忆沙箱 MCP、
  也继承这套 hook；不关掉的话 `stop` 门禁会逼它自己 `memory_remember` 一条，而机器人随后
  还会写一条，**一次问答落两条**，标题和 tags 还各不相同。嵌套 agent 只负责产出一段文本，
  记忆由调用方统一写，所以 `core/llm.py` spawn 时打上这个环境变量

注意**不要**再往本仓库放项目级 `.cursor/hooks.json`——两份都在会重复执行。
`sessionStart` 注入要新开对话才生效；预取与读侧门禁存盘即生效。

关于全局规则的坑：Cursor 不支持 `~/.cursor/rules/*.mdc`（静默忽略），User Rules 只能在
**Customize → Rules** 手填且不随 profile 导出。

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

## 知识库：整篇文档也能被召回

记忆是「一问一答」，适合放结论；而团队里大量口径、模板、流程写在飞书文档里，
摘不成一句话。知识库这一层专门放这种整篇内容：文档按小节切成 600~900 字的块，
每块单独算向量，提问时把相关那几段捞出来当参考。

**为什么不直接存成记忆**：一篇几千字的文档整篇算一条向量等于没有向量
（什么问题都能沾一点、什么都不准），几百个块还会淹掉侧栏的「已记住」列表。
所以它是与长时记忆平级的一层（`core/knowledge.py`），共用同一个 embedder，
只在软召回阶段汇合。

**四种入库方式：**

1. BloomBox 左侧「知识库」tab，粘贴飞书文档链接回车即可（同步抓取，长文要等几秒）
2. 写记忆时正文里带了飞书链接，后台会自动把那篇抓进来——不必手动录一遍
3. **补录存量**：第 2 条只在写记忆那一刻触发，启用知识库之前攒下的记忆里的链接一篇都没抓过。
   用 `python3 main.py knowledge-backfill` 全量扫一遍长时记忆补齐（`--dry-run` 先看会抓哪些、
   `--refresh` 连已入库的也重抓）；界面上是知识库 tab 的「从记忆补录」按钮（走后台队列，
   抓完列表自动刷新），MCP 侧是 `memory_knowledge_backfill`
4. **评论机器人在哪篇文档里回过话，那篇就自动进来**（见「文档评论里也能 @ 它」）

去重认两种 token：wiki 链接的 token 和飞书解析出的 docx `document_id` 不是一回事，
两个都会记在文档上，所以同一篇不会因为「这次是用 wiki 链接引的」而被重抓或存成两份。

自动抓取走后台队列：`fetch_feishu_document` 要刷 token 再拉全文、几秒起步，
而 `remember` 会被 MCP 和机器人同步调用，卡在那里会让每次记忆都变慢。
抓失败只在文档上留 `last_error`（tab 里标红），不影响记忆写入。

**召回时怎么出现**：`memory_prepare` / `memory_ask` 的 `context_pack` 里，
知识库片段单独成节，标成「知识库原文」并给出链接与小节名。它们**不是记忆条目**，
没有记忆 id，不能 `memory_update` / `memory_delete`——内容不对就去改原文档再重新拉取。
硬命中（`ask_local`）不接知识库：那个语义是「记忆库里有现成结论」，
不该把一段文档原文当答案回给用户。

**同一篇只有一份**：去重按飞书 `document_id`，反复录入是更新而不是堆积，
wiki 链接与 docx 链接指向同一篇时也不会各存一份。

**备份连着记忆一起做**：`backup` / `memory_backup` / 工具栏「备份长时」都会在
`backups/` 里落两个配对文件 —— `declarative_<时间戳>.json` 和
`knowledge_<同一时间戳>.json`，`restore` 按时间戳找到那一对一并恢复
（老备份没有知识库快照时知识库不动，不会被清空）。知识库快照**不存向量**：
一个块的 256 维向量写成 JSON 比正文还长七八倍，而向量本来就能从正文重算，
少存这一份还顺带解决了换 embedding 维度后老备份没法恢复的问题。

| 用途 | MCP 工具 | CLI | 界面 |
| --- | --- | --- | --- |
| 收一篇文档进知识库 | `memory_knowledge_add` | `knowledge-add <链接>` | 知识库 tab 的输入框 |
| 看已收录了哪些 | `memory_knowledge_list` | `knowledge-list` | 知识库 tab 列表 |
| 补录记忆里的存量链接 | `memory_knowledge_backfill` | `knowledge-backfill` | 「从记忆补录」按钮 |
| 重新拉取 / 移出 | — | — | 详情页「重新拉取」「删除」 |

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

独立前端在 `desktop/`（应用名 **BloomBox**），通过 HTTP 调用本机 Python API（默认 `http://127.0.0.1:8765`）。**Tauri 启动时会自动执行** `python3 app_web.py --api-only`（退出时只杀自己拉起的进程）。旧浏览器内嵌 UI 仍可用。

复用已有后端前会先比对能力：从随包 `app_web.py` 里读 `UI_FEATURES`，跑着的实例缺任何一项就判为旧进程，关掉重起。少了这步，升级或新增接口后残留的旧后端会一直被复用——它自称健康，新接口却全 404。

**光比特性名还不够**：`UI_FEATURES` 只在「加了新接口」时才动，而大量改动只落在 `core/` 里（检索、拼包、记忆读写）。那种情况下旧进程 200、特性也齐全，于是被当成新鲜的一直用下去，行为却是上一个版本的——这个坑踩过两次（一次新接口 404，一次 `context_pack` 少了记忆 id，门禁因此发不出可操作的 id）。所以 `/api/health` 还会报一个 `code_stamp`：`app_web.py` + `core/*.py` 的内容指纹（`app_web.py::compute_code_stamp`）。

- 只哈希内容、不看 mtime——Tauri 打包会重写 mtime，用时间戳会每次都判成不一致
- 进程启动时算一次，报的是「自己正在跑的代码」，不是磁盘上的最新代码
- Rust 侧 `api_server.rs::expected_code_stamp` 用**同一算法**算随包源码的指纹再比对；两边各有一个对同一组样例文件断言 `ca0f047bc734` 的测试守着，算法一分叉就会挂
- 任一侧算不出（更早的后端没这个字段、或源码树不完整）就当「无意见」不重启，宁可放过也不误杀好后端；而「更早的后端」由 `UI_FEATURES` 里的 `code_stamp` 一项兜住
- 开发态 `resolve_api_root` 优先仓库根，所以手动起的调试后端不会被换掉；打包时 `beforeBuildCommand` 会先 `npm run sync-api` 同步一份新的到 `resources/api`

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

窗口分左右两栏：左侧只列长时记忆的标题（新记的在上，配搜索框与标签下拉；上百个标签铺成 chip 会把列表整个顶出可视区），点标题在右侧看全文——正文、场景/类型/标签、命中次数与记录时间，可直接编辑或删除。删除键平时隐藏，指到那一行才出现。工具栏里那些会往对话区输出的操作会自动退回对话视图。

**别处写入也看得见**：App / MCP / CLI 共用同一份 `declarative.json`，所以别的项目里 agent 通过 MCP 记下的东西，这里 5 秒内会自己冒出来——标题旁显示「N 条新」，新条目带高亮圆点，对话区留一条「新写入 N 条记忆」。轮询走 `GET /api/long_term_revision`，只 `stat` 文件回一个 `mtime:size`，不解析、不回内容，所以敢高频问；窗口不可见时停下。首次拿到的标记只作基线，不然一进来全量都算新。

桌面端已对齐常用 Web 能力：聊天流式、标签筛选、Agent 模式、短时/长时/状态查看、检索设置、提炼候选、备份/清空/归档、导出知识包、优化问法、主题切换等。

**改配置不用翻目录**：顶栏「配置」按钮直接编辑生效的那份 `config.yaml`（就是 `~/Library/Application Support/MemorySandbox/config.yaml`，不是仓库里那份），模型、机器人白名单、`feishu.doc_bot_enabled` 之类都在这里改。两处注意：

- **密钥显示成 `'********'`**：`app_secret`、`user_access_token`、`llm.api_key` 这些不会摊在界面上；掩码留着不动就保持原值，要换就把整串掩码替换成新值。键名带 token 但值是数字的（如 `user_token_expires_at`）照常显示。
- **保存前先校验**：YAML 解析不过、或某个小节写成了字符串，都会拒绝写入并说明原因，原文件不动；真要写时先备份到 `config.yaml.bak-edit`，再原子替换（这文件里存着飞书 token，写坏了得重新授权）。标量类型写错（`top_k` 填成中文）拦不住，那要到用的时候才暴露。

配置是进程启动时读的，所以保存后 BloomBox 要重启，机器人在它自己的弹窗里点「重启」。

### CLI 子命令

| 命令 | 说明 |
|------|------|
| `ask QUERY` | 提问（未命中可走沙箱 LLM）；加 `--local` 则只查本地 |
| `prepare QUERY` | 对齐 MCP：拼接后缀后只查本地 |
| `remember Q A` | 写入长时记忆 |
| `list` | 列出记忆（`--layer working\|long_term\|all`） |
| `status` | 各层统计 |
| `backup` / `restore` | 备份 / 恢复长时记忆与知识库（两者配对，见「知识库」一节） |
| `update --id\|--question ANSWER` | 原地修正一条（结论过时了用这个；可 `--new-question` / `--tag` / `--kind`） |
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
| `记一下 <内容>` / `把 <内容> 存到记忆库` / `<内容>，记下来` | 写入长时记忆，说人话就行 |
| `记住：问题 => 答案` | 想自己分问答时用 |
| `记一下这个结论` | 只有指代时，记的是刚才那一问一答 |
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
| 读正文纯文本（含画板） | `memory_feishu_read` | `feishu-read` |
| 只读预览标题与块数（不含正文） | `memory_feishu_preview` | —（CLI 在确认前自动做） |
| 读评论 | `memory_feishu_list_comments` | `feishu-comments` |
| 加评论 / 回复评论 | `memory_feishu_comment` | `feishu-comment` |
| 新建文档 | `memory_feishu_create_doc` | `feishu-create-doc` |
| 改正文 | `memory_feishu_edit_body`（`mode=append`/`replace`） | `feishu-edit-body` |
| 改 wiki 标题 | `memory_feishu_set_title` | `feishu-set-title` |
| 新建画板（可顺手画流程图） | `memory_feishu_create_board` | `feishu-create-board` |
| 往已有画板画流程图 | `memory_feishu_board_draw` | `feishu-board-draw` |
| 列出文档里的画板与 id | `memory_feishu_list_boards` | `feishu-boards` |

三个读工具容易混，按需要的东西选：

- `memory_feishu_read` —— **要正文**（分析需求、照着写文档）。返回纯文本，默认单次最多 30000 字，
  超长时给 `next_offset`，用同一链接带 `offset` 续读，直到 `next_offset` 为 `null`
- `memory_feishu_preview` —— 只要「是哪一篇、多少块」，**不返回正文**，用于写操作前确认目标、省 token
- `memory_feishu_bookmark` —— 想把文档存成待确认记忆候选，正文会被截断到约 1200 字

### 正文之外的组件（画板 / 图片 / 表格）

docx 的 `raw_content` 只收文字块，但**并非所有非文字内容都会丢**。实测一篇 828 块的技术方案文档：

| 东西 | 块类型 | 现状 |
|------|--------|------|
| 文档小组件 add_ons（mermaid 时序图、流程图） | 40 | 源码**本来就在正文里**，直接能读到，无需额外权限 |
| 画板 | 43 | 独立资源，正文里只有一个 token；`memory_feishu_read` 默认调画板接口读成文字附在末尾 |
| 图片 | 27 | 只有 token，未接入 |
| 电子表格 / 多维表格 / 思维笔记 | 30 / 18 / 29 | 只有 token，未接入 |

画板会渲染成缩进的图形列表 + 连线列表（`A --是--> B`），足以还原流程走向。读不到的组件会在附录里显式写出「未读取 + 缺什么」，不会静默消失。

`memory_feishu_read` 的 `include_widgets` 默认 `true`；只要正文、且确认没有画板时设 `false`，可省两次请求。CLI 对应 `python3 main.py feishu-read <链接> [--no-widgets]`。

读画板需要开放平台开通「查看画板节点（`board:whiteboard:node:read`）」**并重新授权**（scope 固定在 token 里）。没开时附录里会直接提示开哪一项。

> 加权限的顺序不能反：先在开放平台开通，再 `python3 scripts/feishu_login.py`。反过来会让授权页整体报 20027 —— 请求了应用没开通的 scope，整次授权都失败，不只是那一项拿不到。

写工具的 `confirmed` 都是 **required 且必须为 `true`**，漏传或传 `false` 都会直接报错、一个请求都不发出——
和 CLI 的交互确认是同一层门禁，AI 只有在你本轮明确同意后才可以传 `true`。改完 `mcp_server.py` 记得重启 MCP，
否则外部工具握手拿到的还是旧工具清单。

### 建画板、画流程图

飞书**没有「独立画板文件」这种东西**：开放平台文档写明 board 只有节点级接口，画板永远是某篇文档里
`block_type=43` 的一个块，那个块载荷里的 `board.token` 就是 `whiteboard_id`（界面上看不到）。
所以「新建画板」= 往文档追加一个画板块，「画内容」= 往 `whiteboard_id` 批量创建节点。

```bash
# 新建一篇文档，里面放一个画板，顺手画一条竖着的流程
python3 main.py feishu-create-board --title "发布流程" \
  --step "提交 MR" --step "CI 通过" --step "灰度" --step "全量" --label "" --label "观察 1 天"

# 画板插进已有文档，横着画
python3 main.py feishu-create-board --url <文档链接> --direction right --step A --step B

# 往已有画板里追加（先查 id，界面上看不见）
python3 main.py feishu-boards <文档链接>
python3 main.py feishu-board-draw <whiteboard_id> --step 收到告警 --step 定位 --step 修复
```

- `--shape` 可选 `round_rect`（默认）/ `rect` / `ellipse` / `diamond` / `parallelogram`
- `--label` 标在连线上，第 i 个标在第 i 与 i+1 个方框之间
- 内容是**追加**的，不会清掉画板上原有图形；一次最多 3000 个节点
- 建画板走的是 docx 创建块接口，**不需要新权限**；但**往画板里画东西需要
  「创建画板节点（`board:whiteboard:node:create`）」**，缺了会报错并告诉你开哪一项。
  开通后要重跑 `python3 scripts/feishu_login.py`——scope 固定在 token 里，光在后台勾上没用
- 画板建出来但内容没画上时，返回里仍带 `whiteboard_id`：这个半成品得让你看得见，好去清理
- 建画板会自动落一条「画板记录」长时记忆（与正文、评论的问法互不覆盖）；
  往已有画板追加图形不自动落库，因为那没有稳定的「哪篇文档」可挂

### 评论

```bash
python3 main.py feishu-comments <链接>                      # 列出全部评论（只读）
python3 main.py feishu-comment <链接> "这里建议补充埋点" --on "要评论的原文片段"   # 局部评论（写）
python3 main.py feishu-comment <链接> "整篇看下来还行"        # 全文评论，显示在文档底部（写）
python3 main.py feishu-comment <链接> "同意" --reply-to <comment_id>   # 回复某条评论
```

需 `docs:document.comment:read`（读）+ `docs:document.comment:create`（加/回复）。
聚合权限 `docs:doc`、`drive:drive` 也能调通，但范围大到整个云空间，不建议申请。

三种写法走的**不是同一个接口**：

| | 显示位置 | 接口 | 怎么发 |
|---|---|---|---|
| 局部评论（划词评论） | 正文旁边，带引用、可定位 | `POST /drive/v1/files/:token/new_comments`（v2 协议） | `--on` 或 `--block-id` |
| 全文评论 | 文档**最底部** | `POST /drive/v1/files/:token/comments`（v1） | 都不传 |
| 回复已有评论 | 那条评论**串里** | `POST /drive/v1/files/:token/comments/:comment_id/replies` | `--reply-to` |

v1 的 `comments` 接口文档标题就叫「添加全文评论」，明说不支持局部评论；即使按响应体字段
去传 `is_whole` / `quote` 也会被**静默忽略**，永远建出全文评论。要锚定到某段文字，必须走
v2 的 `new_comments` 并传 `anchor.block_id`。

**回复同理，而且这个坑更隐蔽**：`comments` 的响应体里有 `comment_id`「如填写则视为回复已有
评论」，但**请求体规范里没有这个字段**，塞进去会被静默忽略——于是每条「回复」都变成文档底部
一条新的全文评论，看着像机器人不肯回到串里（2026-08-07 实测并修掉）。回复必须打
`comments/{comment_id}/replies`，body 是 `{"content": {"elements": [...]}}`，
不是全文评论那套 `reply_list`；返回的是 `reply_id`（贴表情要用它），评论串还是原来那条。

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
| BloomBox 里点按钮报 `TypeError: Load failed` | WKWebView 把「连不上后端」和「响应被 CORS 拦掉」都报成这一句。多为旧后端仍在 `8765` 上跑（新接口 404，而 404 以前不带 CORS 头就被浏览器整个拦掉）。现在错误响应带 CORS 头并直说是旧后端；重启 BloomBox 或 `python3 app_web.py --api-only` 即可 |
| 改了 `core/` 但行为没变 | 后端是常驻进程，import 时的代码不会热更。现在 BloomBox 启动会按 `code_stamp` 自动换掉；手动可 `curl -X POST 127.0.0.1:8765/api/shutdown` 后重起 `python3 app_web.py --api-only`。MCP 同理，要在 Cursor 里刷新一次 |
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

## 飞书机器人（长连接）

在飞书里私聊机器人、或群里 @ 它，就能查同一份长时记忆，也能顺手写一条——
和 BloomBox、Cursor MCP 共用一个库，这边写完那边立刻能查到。

```bash
pip install lark-oapi
python3 feishu_bot.py --check   # 体检：凭证、白名单、SDK
python3 feishu_bot.py           # 前台启动，Ctrl+C 退出
```

### 用按钮启停，不必守着终端

机器人是常驻进程，占着一个终端窗口不合适——关掉窗口它就没了。所以 BloomBox
顶栏（主题、「记忆」旁边）有个**「飞书机器人」**，按钮上那颗点绿了就是在跑；
点开显示 PID、白名单人数、日志尾巴，配「启动 / 重启 / 停止」三个按钮。
没有终端时用 CLI 也一样：

```bash
python3 main.py bot-status    # 在不在跑（未运行时退出码 1，可以拿去做监控）
python3 main.py bot-start     # 后台启动，日志写文件
python3 main.py bot-restart   # 改完配置用这个
python3 main.py bot-stop
```

几个要知道的点：

- 起出来的进程**自成一个会话**：关掉 BloomBox、重启 API 服务，机器人都照跑不误。
  想让它下线只能显式点「停止」——这是故意的，否则每次关窗口飞书那边就掉线
- 日志在 `~/Library/Application Support/MemorySandbox/logs/feishu_bot.log`，
  超过 2MB 下次启动时清一次。**启动后一秒内就退出**（缺 SDK、配置不全）会把日志
  尾巴直接贴在提示里，不用自己去翻文件
- 状态不只看 pidfile：pid 会被系统回收，所以还要比对命令行确认那个号真是机器人。
  你自己在终端里起的那个也认得出来，界面上会标「在 BloomBox 之外启动的」，
  按停止一样停得掉（两个实例同时在跑会把同一条消息回两遍，所以停止会一起收掉）

**为什么必须是长连接**：记忆沙箱跑在本机，没有公网地址。「将事件发送至开发者服务器」
要求开放平台能回调到公网 HTTPS URL，只能靠内网穿透或部署服务器；长连接只要本机能出网。
代价是只支持企业自建应用。

### 接入步骤

1. 开放平台 →「添加应用能力」→ 启用**机器人**
2. 「权限管理」开：`im:message.p2p_msg:readonly`（收单聊）、
   `im:message.group_at_msg:readonly`（收群里 @）、`im:message:send_as_bot`（回消息）、
   `im:message.reactions:write_only`（贴「处理中/完成」表情，可选）、
   `im:message.group_at_msg.include_bot:readonly`（**别的机器人** @ 它也收得到，可选，
   见下文「让别的机器人驱动它」）。
   机器人说话用的是**应用身份 tenant_access_token**，与读文档那套 user token 无关，
   所以加完这几项**不需要**重跑 `feishu_login.py`，发布版本即可
3. 本地先把机器人跑起来（`python3 feishu_bot.py`，或 BloomBox 顶栏「飞书机器人 → 启动」）
4. 「事件与回调 → **事件配置**」→ 添加事件 `im.message.receive_v1` → 订阅方式选
   「**使用长连接接收事件**」→ 保存
5. 私聊机器人任意一句，它会回你的 `open_id`；填进用户配置的
   `feishu.bot_allow_open_ids`，重启机器人

第 3、4 步的顺序不能反：**保存长连接订阅时本地进程必须已连上**，否则那一步直接保存失败。
另外「回调配置」页管的是卡片按钮回传，收消息不看那里。

### 白名单是必须的

机器人默认全公司都能私聊。记忆库里是你的工作结论、内部路径、方案取舍，
所以白名单为空时机器人**不回答任何内容**，只把对方的 `open_id` 告诉他：

```yaml
feishu:
  bot_allow_open_ids: ["ou_xxx"]
```

也可用环境变量临时覆盖：`FEISHU_BOT_ALLOW=ou_a,ou_b`。群聊里同样按 `open_id` 判断，
不在名单里的人 @ 它也拿不到内容。

### 怎么跟它说话

| 说什么 | 做什么 |
|--------|--------|
| 任意一句话 | 查记忆：命中就给答案，另附最多 3 条**别的**相关记忆 |
| `记一下 <内容>` / `把 <内容> 存到记忆库` / `<内容>，记下来` | 写入长时记忆，说人话就行 |
| `记一下：<问题>`<br>`<答案…>` | 想自己分问答就换行写（首行当问题；单行可用 `=>` 或 `\|\|` 分隔） |
| 回复某条消息 + `记一下这个结论` | 把被回复的那条当答案存下来 |
| `状态` | 长时/工作记忆条数、当前场景 |
| `帮助` | 用法 |

「相关记忆」里**不会再出现刚刚答出来的那条**。软召回用的是同一个 query，命中的那条必然
也在里面且分数最高，照单贴出来就成了「先给答案、再把同一段话原样抄一遍」。所以 `_do_ask`
会先按 `meta.hits` 里的 id 摘掉它；工作/程序性记忆命中拿不到 id，就退回比内容
（相等，或长度 ≥20 且被答案包含——短答案碰巧被包含的多半是另一条记忆，不能误删）。
摘完没剩下的就连「相关记忆：」这个头都不打。

想记别人说的话（尤其是报警卡片），就**回复那条消息**再 @ 机器人：事件里只带
`parent_id`，机器人会把原消息拉回来铺平，文本、富文本、**卡片（interactive）**都能取。
「这个结论」只是指代、当问法检索不到，所以问法退回用引用正文的第一行；想自己命名就写
`记一下 xxx 的根因`。

**读群里被引用的消息需要额外的 `im:message.group_msg` 权限**（上面那三项只够收消息、
回消息）。没开通时机器人会把飞书返回的 230027 原样告诉你，而不是存半条垃圾。

**群里只回 @ 到自己的消息。** 开了群消息权限之后，群里每一条都会送到机器人手上，不判断
就会去接别人 @ 另一个机器人的话（真发生过：用户 @ 运维助理，BloomBot 也答了一条）。
所以启动时会调一次 `bot/v3/info` 问飞书「我是谁」，拿到自己的 open_id 和名字，
再跟事件里的 `mentions` 比对；单聊不受影响。取不到自己的 open_id 时**退回照旧全回**并在
启动日志里警告——群里突然全哑没有任何错误信息，比多嘴更难查。

### 让别的机器人驱动它

默认收不到**机器人**发的消息：`im:message.group_at_msg:readonly` 和
`im:message.group_msg` 都只推用户发的，别的机器人 @ 它飞书压根不下发事件。开
`im:message.group_at_msg.include_bot:readonly`（「获取群组中其他机器人和用户@当前机器人的
消息」）之后就收得到，Slardar、Mira 这类可以直接 @ BloomBot 触发落库，不用人转一手。

收得到之后仍有两道闸，都在 `core/bot.py`：

- **认得出自己才放行。** 自己发的消息也会回推，接了就死循环。所以 `parse_event` 只在
  `self_open_id` 已知、且发送人 open_id 与之不同时才接机器人消息；认不出自己就一律丢掉
  （宁可漏接，不能自问自答）。自己的 open_id 是启动时调 `bot/v3/info` 拿的
- **机器人也要过白名单。** 把那个机器人的 `open_id` 配进 `bot_allow_open_ids` 才理它。
  不在名单里就**静默丢弃**，不回白名单话术——那段话是说给人听的（告诉他自己的 open_id
  好去配置），冲着机器人喊只会在群里多一条没人看的噪音

顺带一提，如果想连**没 @ 它的**机器人消息都收，那是另一项
`im:message.group_msg.include_bot:read`。本项目不需要——群里只回 @ 到自己的消息。

**别的机器人发的卡片，谁都读不到正文，这条路是死的。** 实测同一个群拉 22 条历史：sender 是
应用的 `interactive` 一律只有 157~179 字节、只含一个 `image_key` 和空文本的摘要外壳，而用户
自己发的卡片能拿到 6541 字节的完整内容——卡住的是跨应用，不是卡片本身。

曾经以为「换用户身份就能读到」（用户在客户端里本来就看得见全文），**2026-08-06 实测证伪**：
授权 `im:message:readonly` 之后，用户身份返回的字节数与应用身份一模一样，同一个 `image_key`、
同样的空文本。这与权限无关，飞书就是不下发跨应用的卡片正文。所以 `fetch_quoted` 只在应用身份
**直接报错**时才换用户身份重试（缺 `im:message.group_msg` 这类还是有救的）；「空壳就重试」
那段已删除，它只是白打一次接口。

**读不到那张卡片，不等于拿不到上下文。** 别的 AI 机器人能就同一张卡片给出结论，但原因有两种，
别混为一谈（这里曾经写成「它们都只是读了上游消息」，对 Mira 不成立，2026-08-06 更正）：

- **Aime 确实没读到卡片**：它自己回的是「您附的图片是一张火箭装饰图，没有文字内容」，
  看到的和我们一样是占位图；它读的是**上游那条可读的消息**，这条路我们也能走。
- **Mira 是真读到了正文**：它生成的文档里有三处事实（「同一 Session」「/portal/anchor/relation
  页面」「release 1.0.4.2376」）在上游告警原文里逐字搜不到，只存在于那张我们读不到的卡片里。
  Mira 是飞书**一方应用**、不走 `/open-apis`，这条能力任何三方应用都拿不到，
  也没有哪个权限能开出来（`im:chat.*` 一族管的全是群本身的属性，不返回消息正文）。

实测现场：Slardar 的结论卡片是 157 字节空壳，但它前面第 6 条——**人转发进群的**告警卡片——
`sender_type=user`、6541 字节、1115 个可读字，接口照给。所以在我们能走的那条路上，
关键在 `sender_type`，不在卡片。

于是回复别的机器人的卡片说「记下来」时，BloomBot 会顺着这条线自己去补：
`list_chat_messages` 按时间倒序翻到被引用那条，取它**之前**的十来条，丢掉空壳与
「请升级至最新版本客户端」这类占位提示（它抽得出字，比空的更容易混进去），
超预算时按长度降序保留（告警、日志这种长的才是料），拼成上下文交给模型。模型按
「问题：/ 答案：」两行吐结论再落库——不能把整段聊天记录直接当问法存进去，那样既检索不到
也没法看。模型说「信息不足」就什么都不存，退回让你复制粘贴。

这条补救只在**写入意图 + 引用读不出字 + 配了模型**时才触发，否则每条群消息都要多打一倍接口。

**上游没有料的时候，转发一次就有了。** 别的机器人的分析卡片（排查过程、根因定位、建议动作）
只存在于那张读不到的卡片里，上游的告警原文救不了。但卡住的是 `sender_type=app`——
同一张卡片**由人转发一次**，发送人就变成你（`sender_type=user`），飞书照给完整正文
（群里那条告警卡片正是这么来的，6541 字节）。所以读不到时机器人先让你转发、再退而求其次
让你复制，两条路都写在回复里。

### 表情当进度条

飞书没有「机器人正在输入」这种状态，而这边要重载记忆、可能还要回头拉被引用的消息，
慢的时候用户只能盯着空气等。所以机器人接单就给**你那条消息**贴一个
「处理中」（`OnIt`）表情，答完撤掉、换成「完成」（`DONE`）；回复发不出去时换成
`CrossMark`，一眼能看出是失败而不是没看见。

需要额外开 `im:message.reactions:write_only` 权限（或 `im:message`）并发布版本。
没开也不影响使用：贴不上就只在启动日志里提示一次，回复照发。
被忽略的消息（机器人自己发的、重投的、不在白名单的）不会贴表情。

只处理能取出文字的消息，图片/文件忽略。同一条消息重投不会回两遍。
代码：纯逻辑在 `core/bot.py`（不 import SDK，可脱离飞书跑单测），
长连接与发消息在 `feishu_bot.py`。

### 记忆里没有的，交给 agent 现算

以前本地没命中就回一句「本地记忆里没找到」，等于把问题原样退回来。现在会接着交给
`config.llm` 配的模型（本机默认 Cursor 本地 agent）算一个结论，答完在末尾注明
「本地记忆没命中，以上是模型现给的结论，已存进记忆库」——真写进去了才这么说，
`[LLM Error]` 这类失败答复不会假称已存。没配模型（`llm.enabled=false`）时维持原来的话术。

这条链路几十秒起步，而**飞书长连接的事件回调必须 3 秒内返回，否则同一条事件会被重推**，
所以慢任务走进程内的单线程 worker 队列：回调只入队，答案算完再补发；「处理中」的表情
一直挂着，用户知道还在跑。队列满（默认 32）就退回本地检索结果，不会把整轮卡死。

模型超时单独封顶，别让聊天窗口等十分钟：

```yaml
feishu:
  bot_llm_timeout: 150   # 秒；llm.timeout 配得更短就用更短的
```

### 文档评论里也能 @ 它

在飞书文档的评论里写 `@BloomBot …`，它会在**同一条评论串**里回你：

| 你写什么 | 它做什么 |
|----------|----------|
| `@BloomBot 这段的口径是什么` | 查记忆库 + 读文档正文 → 交给模型给结论 → 回到评论串 |
| 划词评论 + `@BloomBot 把「三天」改成「五天」` | **先提案**：「打算把 A 改成 B，还没有改」 |
| 提案下面回 `确认` | 才真的改，改完回一句「已改，现在是…」 |
| 提案下面回 `算了` | 提案作废 |

写死在代码里的安全边界：

- **改正文一律先提案、等本人回「确认」才落笔**，`confirmed=True` 只在确认后那一次调用里传
- 确认词只认明确表态（`确认` / `同意` / `改吧` / `ok` …）。「确认一下这个数对不对」
  **不算**确认——按前缀匹配就会猜着改文档
- 只做两种改动：**换掉一个块**（划词评论自带 quote，能定位）和**文末追加**。
  不做 `mode=replace` 全文重写，那会删掉所有原有块
- 落笔前重新拉那个块比对原文，对不上（别人已经改过）就拒绝并让你重新 @ 一次
- 定位不到、命中多个块、看不懂要改成什么 → 说清楚为什么没动手，绝不猜
- 白名单之外的人 @ 它一律**不响应也不解释**（评论整篇文档的协作者都看得见）
- 提案 24 小时不确认自动作废

接入（在上面 IM 机器人的基础上）：

1. 重跑 `python3 scripts/feishu_login.py`，授权多出来的 `docs:event:subscribe` 与
   `drive:drive.metadata:readonly`（后者用来按 token 问文档的真实链接，见下面「自动进知识库」；
   飞书要求 `drive:drive` 或它，取窄的这个。读评论的 `docs:document.comment:read`、
   发评论的 `docs:document.comment:create` 原本就在）
2. 「事件与回调 → 事件配置」→ 添加事件 `drive.notice.comment_add_v1`，
   **订阅身份选「用户身份订阅」**，订阅方式仍是长连接
3. 打开开关并重启：

```yaml
feishu:
  doc_bot_enabled: true          # 默认关
  doc_bot_trigger: "@BloomBot"   # 评论正文含这个词才响应
  doc_bot_ack_after_seconds: 8   # 表情贴不上时的兜底：超过 8 秒还没算完，回一条「收到」
```

评论区同样**用表情当进度条**：接单给你那条回复贴「处理中」（`Typing`），做完换成
「完成」（`CheckMark`），没答上来 / 改失败换成 `CrossMark`。云文档的表情挂在
**reply_id** 上而不是 comment_id，走 `POST /drive/v2/files/{token}/comments/reaction`，
用已有的 `docs:document.comment:create` 就能调，不必再开权限。表情枚举与 IM 不是一套：
IM 的 `OnIt` / `DONE` 在云文档里不可用。

**表情贴上了就不再发那条「收到」的文字回复**——评论串里每条回复整篇文档的协作者都看得见，
表情安静得多；只有贴不上（权限没开、接口报错）才退回文字回执，`doc_bot_ack_after_seconds`
就是这条退路的阈值。串里与机器人无关的讨论一个表情都不贴。

**它在哪篇文档里说过话，那篇就自动进知识库。** 判断口径是「真的发出去了一条回复」——
入库挂在 `handle_comment` 里 `post()` 这个唯一的回复出口上，而不是逐个分支各调一次
（分支以后还会加，漏一个就是静默不入库）。所以没被 @、不在白名单、串里别人的讨论、
回复发送失败，都不会入库；一条评论串里回了多条也只入一次。附带效果是那篇文档从此在
知识库里，下次启动会被**按文件订阅**、也进评论轮询范围（`_subscribe_knowledge_docs`
与 `_start_comment_poller` 都只认知识库里的文档），不必再手工逐篇订阅。

这条路**按 token 入库，不按链接**：评论事件里只有 `file_token`，而 `docx_url()` 在
`feishu.doc_host` 没配时返回空串，按链接走会静默什么都不做。可点链接另外跟飞书要一次
（`drive/v1/metas/batch_query`，见 `core/feishu.py` 的 `fetch_doc_meta`）而不是按 `doc_host`
拼——同一租户的文档可能分布在不同区域域名下（如 `bytedance.larkoffice.com` 与
`bytedance.sg.larkoffice.com`），拿单个 host 去拼必然拼错一批，拼出个打不开的链接比留空更坏。
取链接需要 `drive:drive.metadata:readonly`（**没重跑授权就取不到**，报 99991679，
日志里会写「已入库，但没取到可点链接」）。要不到链接也照样入库：正文能被召回是主目的，
点不开原文只是体验降级；那篇后来被别的方式（手动录入、记忆里的链接）带着链接再抓一次时，
链接会补上，空链接也不会把已有的覆盖掉。
机器人自己改过正文的那次会**强制重抓**，否则库里留着的正是它刚推翻的旧正文。

启动时会自动调一次 `POST /drive/v1/user/subscription` 订阅（幂等）；失败只警告，
IM 那半边照常工作。`python3 feishu_bot.py --check` 能看到评论机器人是开是关。

两个必须知道的前提：

- **评论与表情都用应用身份发，所以文档里署名是 BloomBot**，不是你本人。评论接口
  （`comments` / `new_comments` / `comments/reaction`）两种 token 都收，**署名跟着 token 走**：
  用 `tenant_access_token` 就是机器人，用 `user_access_token` 就是你。定位文档那步仍走
  user token（wiki 节点解析靠本人权限），只有最后那一次写请求用应用身份；应用对这篇文档
  没权限时自动退回本人身份，回复不会因此发不出去。
  自动回复仍带 `🤖 BloomBot 自动回复` 前缀——它是「别自己触发自己」的闸
  （`is_bot_reply` 认的就是这几个字），不是署名的替代品。
  `memory_feishu_comment` 这类**你让 AI 代做评审**的评论仍署你的名（`as_app` 默认 `False`），
  那些意见是你在说话。
- 事件是「**你**收到评论通知」时才推送的，所以只有你能收到通知的文档才管用；
  别人在你完全无关的文档里 @BloomBot，本机收不到任何事件。

默认关是有意的：打开就等于预先同意「白名单成员在评论里点名时可以自动回这一串」，
而改正文仍然逐条确认。代码：纯逻辑在 `core/doc_bot.py`、落盘状态在 `core/doc_bot_state.py`，
编排在 `feishu_bot.py` 的 `handle_comment`。

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
├── feishu_bot.py        # 飞书机器人长连接入口（需 lark-oapi）
├── desktop/             # React + Vite + Tauri 桌面前端
├── core/
│   ├── sensory.py       # 感觉记忆
│   ├── working.py       # 工作记忆
│   ├── long_term.py     # 长时记忆
│   ├── knowledge.py     # 知识库：整篇文档切块存储与块级检索
│   ├── knowledge_chunk.py   # 按标题/段落切块
│   ├── knowledge_ingest.py  # 飞书文档抓取入库（同步入口 + 后台队列）
│   ├── embedding.py     # 本地哈希向量（无模型下载）
│   ├── rules.py         # 轻量规则引擎
│   ├── llm.py           # 大模型适配
│   └── sandbox.py       # 主编排 chat()
├── examples/demo.py
├── .agents/skills/      # vendored 第三方 skill（改 UI 的规范，见下）
├── skills-lock.json     # 上面这些 skill 的来源与哈希
└── data/memory/         # 运行后生成的持久化数据
```

## 改 UI 走 emilkowalski/skills

桌面端和 Web UI 的动效与视觉不靠即兴发挥：仓库里装了
[emilkowalski/skills](https://github.com/emilkowalski/skills)（9 个 skill，
真实文件、已进版本库），`.cursor/rules/ui-craft.mdc` 把它绑到了
`desktop/src/**` 与 `app_web.py` 上——AI 改这些文件之前必须先读
`.agents/skills/emil-design-eng/SKILL.md`，再按任务挑 `animate`、
`review-animations`、`improve-animations` 等。

规则文件里同时记着本仓库与 skill 默认技术栈的差异，避免照抄：没有
framer-motion（只有 React 19 + 原生 CSS，弹簧那部分要用 CSS 实现）、
`App.css` 目前一条 `transition` 都没有（第一次加动效要把 easing token 建到 `:root`）、
主题是 `:root` + `[data-theme]` 两套必须都给、外链必须走 `openExternal.ts`。

升级或重装（内网有两个坑：镜像 403、clone 要写 `.git/hooks`）：

```bash
npx --yes --registry=https://registry.npmjs.org skills@latest add emilkowalski/skills \
  -s '*' -a cursor --copy -y
```

`--copy` 不能省，默认是软链、进不了版本库。`.agents/skills/` 下的文件是产物，
要偏离原作者建议就改 `.cursor/rules/ui-craft.mdc`，别改 skill 正文。

## 设计取舍（本地落地）

1. **Embedding**：默认本地特征哈希向量，免下载模型，保证同库可复现匹配。
2. **向量库**：默认 JSON 持久化，零外部服务；数据量变大时可再换 Chroma/FAISS。
3. **LLM**：可插拔；未配置时用 MockLLM，保证离线链路完整可跑。
4. **记忆强化**：命中提升 `weight`；高频短问答可沉淀进工作记忆 FAQ。
