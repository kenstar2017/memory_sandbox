# Memory Sandbox

English | [中文](./README_zh.md)

A local layered memory system that mirrors human **sensory → working → long-term** memory.

**Prefer sandbox retrieval/reasoning first; call a large model only when the sandbox has no useful answer.** Useful when switching dev environments daily: reuse local experience and cut token spend.

## Architecture

```
User input
  → Sensory memory (TTL buffer, noise filtering)
  → Working memory (sliding window + rules/context) ──hit──→ direct output
  → Long-term memory (vector store + procedural rules) ──hit──→ direct output
  → LLM (only when sandbox has no answer)
  → Write results back to all three layers (consolidation)
```

| Layer | Implementation | Lifetime | Role |
|------|------|------|------|
| Sensory | In-memory dict | 2–5s TTL | Accept raw input, filter noise |
| Working | Sliding window (default 7) | Session / idle clear | Short context, local rule inference |
| Long-term | JSON vector store + rules | Persistent | History Q&A, dev knowledge, templates |

## Cursor Integration (recommended)

Use **MCP** so the Cursor Agent checks the local Memory Sandbox first, then decides whether to dig deeper—fewer repeated questions and tokens.

### 1. One-click global config (any project)

```bash
cd memory_sandbox
./scripts/install_cursor_mcp.sh
```

Writes `~/.cursor/mcp.json`.

This repo also ships a project-level config: `.cursor/mcp.json`.

### 2. Restart Cursor

Open **Settings → MCP** and confirm `memory-sandbox` is connected (green).

### 3. How to use in chat

Examples:

- “Use Memory Sandbox to check how to start agency”
- “Remember this: local mock port is 3001”

The Agent will call these tools:

| Tool | Role |
|------|------|
| `memory_prepare` | **Preferred each turn**: append “record to long-term…” then search; returns `references` / `context_pack` |
| `memory_ask` | Search as-is (no suffix assembly) |
| `memory_remember` | Persist reusable conclusions (optional `tags`) |
| `memory_forget` | Active forgetting |
| `memory_status` | Memory stats |
| `memory_set_scene` | Switch scene (e.g. `dev`) |

Project rule `.cursor/rules/memory-sandbox.mdc` guides the Agent: **`memory_prepare` first**; treat `references` / `context_pack` as background and combine with the current repo; for feature work do not short-circuit on a hard hit; only reuse `answer` for pure factual Q&A when `hit_local`; always `memory_remember` stable conclusions.

### Tags & types (easier to find)

- Tag memories (`feishu`, `frontend`, or `#tag` in text)
- Optional kinds: QA / command / path / env / pitfall / decision
- Hits include score + reasons so you can verify or forget

### Less friction, safer writes

- Extract candidates from terminal/logs, then confirm before saving
- Auto-redact tokens/secrets/private keys on write
- Web sidebar: filter by tag; edit tags/kind in the modal

### Better search & sharing

- Hybrid retrieval: vectors + keywords + BM25 (Web toolbar “Retrieval settings”, with per-field help; also saved to user `config.yaml`)
- Soft-decay rarely used items; archive when needed
- Export scrubbed knowledge packs for teammates to import

### Stay in sync with code & Feishu

- `git-check`: flag memories that may be outdated after git changes
- `review-suggest`: turn recent commits into habit/convention candidates
- `feishu-bookmark`: turn Feishu docs into confirm-to-save candidates
- `pack-list`: list locally exported packs

Memory data is shared with the desktop app:

`~/Library/Application Support/MemorySandbox/`

## Mac Installer (.dmg)

A double-clickable local app is available:

```text
dist/MemorySandbox-0.1.1-mac.dmg
```

**Install:**

1. Open the DMG  
2. Drag `MemorySandbox` to `Applications`  
3. Launch “Memory Sandbox”; it opens the local UI in the browser (`http://127.0.0.1:8765`)

Note: On macOS 26, system tkinter/Tcl can crash, so the app uses a local Web UI (no tkinter).

If macOS blocks an unidentified developer: right-click the app → Open → Open anyway.

Memory data directory: `~/Library/Application Support/MemorySandbox/`

**Rebuild:**

```bash
./scripts/build_dmg.sh
```

Current build targets Apple Silicon (arm64).

## Quick Start (from source)

```bash
cd memory_sandbox
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Local Web UI (recommended; macOS 26 compatible)
python3 app_web.py
# API-only for desktop / split frontend (no browser auto-open)
# python3 app_web.py --api-only
# Legacy tkinter GUI (may crash on macOS 26 system Python)
# python3 app_gui.py

# Desktop (React + Tauri): terminal 1 → api-only; terminal 2 →
#   cd desktop && npm i && npm run tauri:dev
# See README_zh.md / desktop/README.md

# CLI (shares user memory dir with MCP/Web)
./scripts/memory ask "how to start revenue locally"
./scripts/memory ask --local "PK component"   # local only, no sandbox LLM
./scripts/memory prepare "how to start revenue"  # append long-term suffix, local only
./scripts/memory remember "Q" "A" --scene dev --tag feishu
./scripts/memory list --layer long_term
./scripts/memory status
./scripts/memory backup
./scripts/memory                           # interactive mode

# Equivalent:
python3 main.py ask "how to start the local frontend"

# Seed / demo
python3 main.py seed
python3 examples/demo.py
```

Optional: link `scripts/memory` onto PATH:

```bash
ln -sf "$(pwd)/scripts/memory" /usr/local/bin/memory-sandbox
```

### CLI subcommands

| Command | Description |
|------|------|
| `ask QUERY` | Ask (may fall back to sandbox LLM); `--local` = local only |
| `prepare QUERY` | MCP-aligned: append suffix, query local only |
| `remember Q A` | Write long-term memory |
| `list` | List memories (`--layer working\|long_term\|all`) |
| `status` | Per-layer stats |
| `backup` / `restore` | Backup / restore long-term memory |
| `delete --id\|--question` | Delete one entry |
| `forget [keyword]` | Forget by keyword or clear a layer (`--yes` to clear) |
| `clear-long --yes` | Clear long-term (optional `--backup-first`) |
| `scene NAME` | Switch scene |
| `agent-mode [ask\|plan\|agent]` | View/switch local Cursor Agent mode |
| `seed` / `reoptimize` | Seed memory / refresh index |
| `interactive` | Interactive mode |

Default memory dir: `~/Library/Application Support/MemorySandbox/memory`  
For in-project `data/memory`, add `--project-memory`.

### Interactive commands

| Command | Description |
|------|------|
| `记住：问题 => 答案` | Write long-term memory |
| `记住：某条知识` | Persist a knowledge snippet |
| `忘记刚才内容` | Clear working/sensory memory |
| `忘记：关键词` | Clear layers by keyword |
| `清空工作记忆` | Clear sliding window only |
| `切换场景：dev` | Context-weighted retrieval |
| `切换Agent模式：ask\|plan\|agent` | Local LLM read-only / plan / writable |
| `飞书登录` | Browser OAuth for Feishu doc reading |
| `查看记忆状态` | Print per-layer stats |
| `帮助` | Built-in rule-engine help |

Interactive mode shows colored stage progress and a spinner on stderr (retrieve → LLM fallback → Local Agent / Cloud); answers go to stdout. Progress on stderr, results on stdout; `NO_COLOR=1` disables color; `--json` skips human formatting.

## Configuration

See `config.yaml`:

- `sensory.ttl`: sensory expiry in seconds
- `working.chunk_size`: working-memory window size
- `long_term.similarity_threshold`: long-term hit threshold (suggest 0.65–0.75)
- `long_term.persist_dir`: persistence dir (default `data/memory`)
- `llm.provider`: `mock` (offline stub) | `cursor` | `openai_compatible`
- `llm.runtime` (cursor only): `local` (local `agent` CLI, can read disk) | `cloud` (no repo; cannot scan local source)
- `feishu.*`: Feishu wiki/docx reading (see “Feishu / Lark document reading” below)

## Feishu / Lark document reading

When a message contains a Feishu wiki/docx URL, the sandbox can **fetch the body** before LLM fallback.

- Does **not** depend on Cursor/Trae MCP; does **not** change `~/.cursor/mcp.json`
- Secrets stay in the local user config — **do not commit**
- Code: `core/feishu.py`, `core/feishu_oauth.py`; scripts: `scripts/feishu_login.py`, `scripts/configure_feishu.sh`

### How it works

```
Message with Feishu URL
  → local memory miss
  → parse wiki/docx URL
  → (optional) refresh user_access_token
  → OpenAPI wiki node → docx raw_content
  → inject into LLM context → answer
```

| Topic | Behavior |
|-------|----------|
| When | Web/CLI LLM fallback only; MCP `memory_prepare` does **not** fetch |
| Working memory | Feishu-URL questions skip reusing prior answers (helps retries) |
| Failures | Auth/fetch failures are not persisted to working/long-term memory |
| Stored question | Rewritten from **doc title + user intent** (URL kept for token matching); Web “complete answer” modal lets you edit Q / “optimize question” |
| No auto-save | After a successful Feishu fetch, long-term is **not** written until you confirm; edit Q then save once (same token / id updates in place) |

### Config locations

| File | Role |
|------|------|
| Repo `config.yaml` | Template (`feishu.enabled: false` by default) |
| **`~/Library/Application Support/MemorySandbox/config.yaml`** | **Effective** user config |

Env (optional): `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_USER_ACCESS_TOKEN` / `FEISHU_API_BASE`.

### Setup

1. Create an app in the [Feishu Open Platform](https://open.feishu.cn/). Suggested scopes: `offline_access`, `docs:document.content:read`, `wiki:wiki:readonly` / `wiki:node:read`.
2. Add redirect URL (must match config exactly):

```text
http://127.0.0.1:18765/feishu/callback
```

This is the **OAuth login callback**, not the Web UI (`8765`) and not an event-subscription URL.

3. User config:

```yaml
feishu:
  enabled: true
  app_id: "cli_xxx"
  app_secret: "xxx"
  redirect_uri: "http://127.0.0.1:18765/feishu/callback"
```

Or: `FEISHU_APP_ID=... FEISHU_APP_SECRET=... ./scripts/configure_feishu.sh`

4. Browser OAuth (`user_access_token` is not shown in the admin console):

```bash
python3 scripts/feishu_login.py
# or send in chat: 飞书登录
```

Stores `user_access_token` + `refresh_token`; auto-refresh on expiry.

5. Restart Web/CLI and ask with a Feishu link.

### Related commands

| Command | Meaning |
|---------|---------|
| `飞书登录` / `飞书授权` / `登录飞书` | Browser OAuth |
| `重试` / `再试一次` / `重新分析` | Skip working-memory reuse |
| `清空工作记忆` | Clear short-term window |

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `99991668` | Re-run `feishu_login.py` |
| `131006` | Need user token for personal docs, or authorize the doc to the app |
| Missing wiki scope | Enable wiki read scopes on the app |
| Callback timeout | Redirect URL must match; port `18765` free |
| Stale answer | Clear working memory or say「重试」; restart Web after code changes |
| Repo config ignored | Edit Application Support user config |

### Cursor model (when memory misses)

If sensory / working / long-term all miss, CLI/Web fall back to Cursor.

**Default `runtime: local`**: calls local `agent` / `cursor-agent`, `--workspace` = `llm.cwd` (empty = cwd at launch), default `--mode ask` (read-only), can scan/explain local source.

**`runtime: cloud`**: Cursor Cloud agent with no repo — **cannot see local disk**.

Prefer putting secrets in user config (do not commit):

`~/Library/Application Support/MemorySandbox/config.yaml`

```yaml
llm:
  enabled: true
  provider: cursor
  runtime: local          # local=read disk | cloud=no repo
  api_key: "crsr_..."
  model: ""               # optional
  timeout: 600
  cwd: ""                 # empty = project where CLI was started
  agent_mode: ask         # ask=read-only; plan=plan; writable via CLI/Web → agent
```

Env vars also work: `CURSOR_API_KEY` / `CURSOR_MODEL` / `CURSOR_CWD`.

Requires Cursor Agent CLI (`agent` on PATH). If missing, it errors clearly and does not silently switch to Cloud.

**Switch Agent mode (Ask / Plan / Agent)**

| Entry | Usage |
|------|------|
| CLI | `memory-sandbox agent-mode` to view; `memory-sandbox agent-mode agent` to switch & persist |
| CLI interactive | `切换Agent模式：ask` / `plan` / `agent` |
| CLI one-shot | `memory-sandbox ask --agent-mode agent "…"` (not persisted) |
| Web | Toolbar “Agent mode” dropdown |

`agent` = full tools writable (uses `--force`; use with care); `ask`/`plan` = read-only. Settings go to `~/Library/Application Support/MemorySandbox/config.yaml`.

Sandbox fallback Local/Cloud Agents **must not `git push` or open PRs**; push from your own terminal when needed.

### OpenAI-compatible gateway

```yaml
llm:
  enabled: true
  provider: openai_compatible
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
  model: "gpt-4o-mini"
```

Env vars: `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`.

## Code entry

```python
from core import MemorySandbox

sb = MemorySandbox()                 # loads config.yaml
result = sb.chat("what is the local mock port")
print(result.answer, result.source)  # source: working | long_term | llm | ...
```

## Layout

```
memory_sandbox/
├── config.yaml          # thresholds & LLM config
├── main.py              # CLI
├── core/
│   ├── sensory.py       # sensory memory
│   ├── working.py       # working memory
│   ├── long_term.py     # long-term memory
│   ├── embedding.py     # local hash vectors (no model download)
│   ├── rules.py         # lightweight rule engine
│   ├── llm.py           # LLM adapters
│   └── sandbox.py       # main chat() orchestration
├── examples/demo.py
└── data/memory/         # persistence after runs
```

## Design trade-offs (local-first)

1. **Embedding**: default local feature-hash vectors—no model download; reproducible matches in the same store.
2. **Vector store**: default JSON persistence, zero external services; swap to Chroma/FAISS later if needed.
3. **LLM**: pluggable; MockLLM when unset so the offline path still runs.
4. **Reinforcement**: hits raise `weight`; frequent short Q&A can settle into working-memory FAQ.
