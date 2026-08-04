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

### 4. Memory gate: enforce "search first, record after" in every project

That rule is **project-scoped**, so it only applies inside this repo. Elsewhere the agent knows
neither to search first nor to record afterwards: a whole debugging session is lost, or it writes a
document from scratch while the template was already in memory. The gate uses **user-level hooks**
(`~/.cursor/hooks.json` + `~/.cursor/hooks/`, active in every project), one on each side.

Four ways to install the same thing (logic lives in `core/cursor_hooks.py`):

| Path | How |
|------|-----|
| BloomBox first launch | Asks once; installs on consent and never nags again if declined |
| BloomBox toolbar | "AI 门禁" shows status and can enable, refresh, or disable |
| CLI | `python3 main.py hooks-install` / `hooks-status` / `hooks-uninstall` |
| Script | `./scripts/install_cursor_hooks.sh` (`--status` / `--uninstall`) |

Installing **merges** into `~/.cursor/hooks.json`: it only claims entries whose command mentions one
of its own scripts, so your own hooks are never touched, and the original file is backed up to
`hooks.json.bak-<timestamp>` first. Re-running never duplicates entries; changed script contents are
reported as "needs update" (detected by content hash, not a hand-maintained version number).

| Script | Event | Purpose |
|--------|-------|---------|
| `memory-session-context.py` | `sessionStart` | Injects the calling protocol via `additional_context` into every conversation's initial system context |
| `memory-require-prepare.py` | `preToolUse` | About to change something without having searched memory this turn → denied once, told to call `memory_prepare` first |
| `memory-mark.py` | `postToolUse` | Writes `.prepared` after prepare/ask, `.remembered` after remember |
| `memory-ensure-remember.py` | `stop` | Follow-up if nothing was recorded; clears read-side markers per turn and prunes state older than 7 days |

- Script sources live in `cursor_hooks/` and are copied into `~/.cursor/hooks/` on install. They use
  **stdlib only**, so once installed they are fully decoupled from this repo: delete the repo or
  uninstall BloomBox and the hooks keep working (a unit test guards that constraint)
- Markers live in `~/.cursor/memory-sandbox-hook-state/` keyed by `conversation_id` and are cleared
  each `stop`, so it is "search every turn, record every turn"
- Only `Write` / `Delete` / `Task` and Feishu writes are gated; `Read` / `Grep` / `Shell` and all
  `memory_*` tools pass through
- At most one denial per turn, so an unavailable MCP server can never deadlock the turn;
  `loop_limit: 1` keeps the stop hook to a single nudge
- Every script fails open (`{}` / `permission: allow`, exit 0) and can never block normal work

Do **not** add a project-level `.cursor/hooks.json` back to this repo — having both would run twice.
The `sessionStart` injection only applies to newly opened conversations; the read-side gate is live
as soon as the file is saved.

Two traps around global rules: Cursor does not support `~/.cursor/rules/*.mdc` (silently ignored),
and User Rules exist only in **Customize → Rules** and are not included in profile exports.
`beforeSubmitPrompt` cannot inject context either (it accepts only `continue` / `user_message` and
silently drops extra fields). The `sessionStart` injection above replaces that manual step.

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
- `feishu.*`: Feishu wiki/docx read & write (see “Feishu / Lark documents” below)

## Feishu / Lark documents (read & write)

When a message contains a Feishu wiki/docx URL, the sandbox can **fetch the body** before LLM fallback.
Three **write** capabilities also exist (rename a wiki node, create a doc, edit a doc body) — see
“Writing to Feishu” below; all of them **require your confirmation every time**.

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

1. Create an app in the [Feishu Open Platform](https://open.feishu.cn/). Read-only scopes: `offline_access`, `docs:document.content:read`, `wiki:wiki:readonly` / `wiki:node:read`. For writes add `wiki:node:update` (rename) and/or `docx:document:create` + `docx:document:readonly` + `docx:document:write_only` (create a doc, edit a body). **The console has no `docx:document` entry** — the API docs use that umbrella name, but only the three granular scopes are selectable. Scopes are also split into “app identity (tenant_access_token)” and “user identity (user_access_token)” tabs; this project uses the user token, so **enable them under user identity**.
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

Login prints the scopes that were **actually granted** and lists any you requested but didn't get —
review-gated scopes such as `docx:document:write_only` often stall there, and it's better to see it
now than when an API call fails.

Getting a `refresh_token` (without one you re-login roughly every 2 hours) needs **two** switches:
enable `offline_access` under **Permissions & Scopes**, *and* turn on **“refresh user_access_token”**
under **Security Settings**. The second one is easy to miss — with the scope alone no refresh token
is issued and refreshing fails with `20074`. Publish a version after changing either, then log in again.

5. Restart Web/CLI and ask with a Feishu link.

### Related commands

| Command | Meaning |
|---------|---------|
| `飞书登录` / `飞书授权` / `登录飞书` | Browser OAuth |
| `重试` / `再试一次` / `重新分析` | Skip working-memory reuse |
| `清空工作记忆` | Clear short-term window |

### Writing to Feishu (confirmation required every time)

Feishu docs are usually shared with the team and edits are hard to roll back, so writes are
**deny-by-default**:

- Write functions in `core/feishu.py` take `confirmed=False` by default and **return an error
  without issuing any request** unless `True` is passed explicitly
- The CLI asks for confirmation interactively; `--yes` is for you only
- See `.cursor/rules/memory-sandbox.mdc`: the agent must not initiate writes, and a previous
  approval never counts as approval for the current change

```bash
python3 main.py feishu-comments <URL>                                 # list comments (read-only)
python3 main.py feishu-comment <URL> "please add tracking here"       # add a whole-file comment
python3 main.py feishu-set-title <wiki URL> "<new title>"             # needs wiki:node:update
python3 main.py feishu-create-doc "<title>" --content-file notes.md   # needs :create + :write_only
python3 main.py feishu-edit-body <URL> --append  --content-file notes.md   # append to the end
python3 main.py feishu-edit-body <URL> --replace --content-file notes.md   # wipe body, rewrite
```

Renaming only works on wiki links — a docx direct link carries no `space_id` / `node_token`.
Body edits accept both wiki and docx links, and `--append` / `--replace` must be chosen
explicitly. Before asking for confirmation the CLI does a **read-only** fetch and prints the
target title plus its current block count; `--replace` also spells out how many blocks it will
delete, because editing the wrong document costs more than writing the wrong text. Empty content
is always rejected, so a file that happens to read empty can never wipe a document.

Document bodies accept a Markdown subset. Block level: `#`–`######` headings, `-`/`*` bullets,
`1.` ordered items, ``` code fences, `>` quotes, `---` dividers, and GFM pipe tables
(`| a | b |` followed by `|---|---|`); everything else non-blank becomes a paragraph. Inline:
`**bold**`, `*italic*`, `~~strikethrough~~`, `` `code` ``, and `[text](url)`. Markdown inside code
fences and inline code is left literal, so `**` and `|` survive untouched.

Tables are the one structural exception: a Feishu table is a three-level nest
(table → table_cell → text) that the flat *create blocks* endpoint cannot express, so each table is
written through the *create nested blocks* endpoint in its own request while flat blocks go out in
batches of 50. Both are appended in document order, so mixed content keeps its original sequence.
Column widths are set explicitly, sized per column from its longest cell (CJK counts double, Markdown
markup counts as nothing) to fill the ~800px body area with a 100px floor per column. Without an
explicit `column_width` Feishu splits a small default width evenly and long file paths collapse to
one character per line; with more columns than the budget allows every column keeps the floor and the
table scrolls horizontally instead.
Still unsupported and written as plain text: images, footnotes, task lists, nested list indentation,
and line breaks inside a cell.

Comments need `docs:document.comment:read` (list) and `docs:document.comment:create` (add or
reply). Two kinds of comment exist and they do **not** share an endpoint. An *anchored* comment
(shown next to the passage, carrying a quote) requires the v2 endpoint
`POST /drive/v1/files/:token/new_comments` with `anchor.block_id`; a *whole-file* comment (shown at
the very **bottom** of the document) uses the v1 `POST /drive/v1/files/:token/comments`. The v1
endpoint is literally titled "add a whole-file comment" and **silently ignores** `is_whole` /
`quote` if you pass them, so it can never produce an anchored comment.

Pass `--on "<a distinctive snippet of the original text>"` to anchor: the snippet is matched against
every block (across bold and other inline style boundaries, whitespace-insensitive). If it matches
more than one block the command **fails and lists the candidates** instead of guessing, because
anchoring a comment to the wrong paragraph is worse than not sending it; narrow the snippet or pass
`--block-id`. For review work, prefer one anchored comment per issue over a single whole-file dump.

Reading has no such limit: anchored comments made by others come back too, distinguished by
`is_whole` and `quote` (the highlighted text), with the comment body inside `replies`. Commenting
notifies collaborators, so it sits behind the same confirmation gate as body edits.

The same capabilities are exposed as MCP tools, so external AI tools (Cursor and friends) can use
them without dropping back to a terminal: `memory_feishu_read` (body text),
`memory_feishu_preview` (title + block count only), `memory_feishu_list_comments`,
`memory_feishu_comment`, `memory_feishu_create_doc`,
`memory_feishu_edit_body` (`mode=append`/`replace`) and `memory_feishu_set_title`.

Pick the right reader: `memory_feishu_read` when you need the **body** (returns plain text, at most
`max_chars` per call — default 30000 — with a `next_offset` to continue until it comes back `null`);
`memory_feishu_preview` when you only need to know which document and how many blocks, which
deliberately omits the body to save tokens; `memory_feishu_bookmark` to turn a document into a
pending memory candidate, where the body is truncated to roughly 1200 characters. On all three write tools `confirmed` is **required and must be `true`** —
omitting it or passing `false` fails immediately without issuing a single request, the same gate the
CLI enforces interactively. After editing `mcp_server.py`, restart the MCP server; otherwise clients
keep the tool list from their last handshake.

### Documents you write are recorded automatically

Whenever a create / body edit / rename actually takes effect on the Feishu side, a long-term memory
entry is written automatically (from both MCP and the CLI), so there is no need to call
`memory_remember` for it yourself. The question is always `《title》飞书文档正文与写入记录 <url>`,
matching the shape used for documents read *in*, so one topical query finds both. The answer records
the action, timestamp, link, `document_id`, blocks written/deleted, plus an outline and a ~600
character excerpt (never the full body, which would bloat retrieval). Entries are tagged
`feishu` / `docs` / `doc-write` (`doc-comment` for comments), and editing the same document again
**updates the same entry** instead of piling up duplicates. Body and comments are two *facets* of
one document and are kept as separate entries: dedup includes a "same Feishu token means same
record" rule, so distinct questions alone would not prevent a comment from clobbering the body —
`save_memory(dedup_facet=...)` restricts merging to within a facet. MCP returns the outcome in a `remembered` field; the CLI prints it.

Two edge cases worth knowing: nothing is recorded when the Feishu side was untouched (a refused
confirmation, an expired token), because a change that never happened should not enter the history;
but a half-created document, or a `replace` that deleted the old body and then failed to write, **is**
recorded and flagged as incomplete, since both leave something that needs cleaning up or restoring.
If the bookkeeping write itself fails, the command still reports success and only warns — otherwise a
caller would think the write never landed and retry, creating a duplicate document.

API limits handled for you, but worth knowing: the create API sets the title only (the body needs
a second “create blocks” call, hence the edit scope); a single request handles at most 50 blocks,
so writes and deletes are batched and throttled to 3 requests/second; deletes take a half-open
`[start_index, end_index)` range, and each batch removes the frontmost slice because later blocks
shift down. If a create’s body write fails mid-way the doc already exists and the error carries
its `document_id`; if a `--replace` deletes and then fails to write, the error points you at
Feishu’s version history.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `99991668` | Re-run `feishu_login.py` |
| No `refresh_token` after login (re-login every 2h) | Granting `offline_access` isn't enough — also turn on “refresh user_access_token” in Security Settings, publish, and log in again; a `20074` on refresh has the same cause |
| A scope you enabled is still denied | Check the “not granted” list printed at login: review-gated scope pending, or enabled only under the app-identity tab instead of user identity |
| `131006` | Need user token for personal docs, or authorize the doc to the app; writes also need container edit permission |
| `1770040` / `1770032` | No edit permission on the target folder, or `docx:document:write_only` not enabled |
| Missing wiki scope | Enable wiki read scopes on the app |
| New scope still denied | Scopes are baked into the token — re-run `python3 scripts/feishu_login.py` |
| Consent screen says `20027` “insufficient permission: docx:document” | That umbrella scope no longer exists in the console, so it can never be granted. We now request `docx:document:create` / `:readonly` / `:write_only`, and `_RETIRED_SCOPES` strips a stale `docx:document` from old configs. Also check that (1) the scopes are enabled under the **user identity** tab, (2) `:write_only` needs admin review, and (3) scope changes require publishing a new app version |
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
