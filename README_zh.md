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
- `feishu.*`：飞书 wiki/docx 读取（见下方「飞书文档读取」）

## 飞书文档读取

对话中出现飞书 wiki/docx 链接时，沙箱可在回退 LLM 前 **自动拉取正文**。

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

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建应用，记下 App ID / App Secret。建议权限：`offline_access`、`docs:document.content:read`、`wiki:wiki:readonly` / `wiki:node:read`。
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

5. 重启 Web / CLI，提问带飞书链接的问题即可（进度中应有「飞书文档：拉取…」）。

### 配置字段

| 字段 | 说明 |
|------|------|
| `enabled` | `true` 才启用 |
| `app_id` / `app_secret` | 开放平台应用凭证 |
| `redirect_uri` | 须与开放平台重定向 URL 一致 |
| `user_access_token` / `refresh_token` | OAuth 写入；后者用于续期 |
| `api_base` / `timeout` / `max_chars` | API 根、超时、注入截断长度 |

### 相关指令

| 指令 | 说明 |
|------|------|
| `飞书登录` / `飞书授权` / `登录飞书` | 浏览器 OAuth |
| `重试` / `再试一次` / `重新分析` | 跳过工作记忆复用 |
| `清空工作记忆` | 清掉短时旧结论 |

### 排障

| 现象 | 处理 |
|------|------|
| `99991668` Invalid access token | 再跑 `feishu_login.py` |
| `131006` node permission denied | 个人文档靠 user token；或把文档授权给应用 |
| 缺 wiki scope | 开放平台开通 wiki 读权限 |
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
