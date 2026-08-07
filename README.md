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
| `memory_update` | **Fix a stale memory** in place by id (value changed, spec revised, conclusion overturned) |
| `memory_delete` | Drop an entry that is wholly obsolete |
| `memory_forget` | Active forgetting |
| `memory_status` | Memory stats |
| `memory_set_scene` | Switch scene (e.g. `dev`) |

Project rule `.cursor/rules/memory-sandbox.mdc` guides the Agent: **`memory_prepare` first**; treat `references` / `context_pack` as background and combine with the current repo; for feature work do not short-circuit on a hard hit; only reuse `answer` for pure factual Q&A when `hit_local`; always `memory_remember` stable conclusions.

**Maintaining memory matters as much as writing it.** The worst failure mode of a knowledge base is
not a missing entry but a superseded one: it still gets retrieved and still reads like an authoritative
answer. So every recall carries its `id=` (a field in `references`, printed inside the `context_pack`
text too), and an agent that spots a contradiction can call `memory_update(memory_id=..., answer=...)`
directly. That duty is also wired into the gate (below).

Two deliberate choices in `memory_update`: it **errors instead of creating** when the target cannot be
located (an "update" that lands as an insert leaves both the old and new claim in the store, which
fights at retrieval time and is worse than not updating), and **omitted fields keep their old values**,
so fixing a conclusion means passing only `answer` — no need to restate a carefully tuned question.

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
| `memory-prefetch.py` | `beforeSubmitPrompt` | **Retrieves on your verbatim prompt the moment you hit send**, stashes the pack for delivery, and resets read-side markers for the new turn |
| `memory-session-context.py` | `sessionStart` | Injects the calling protocol via `additional_context` (best effort — see below) |
| `memory-require-prepare.py` | `preToolUse` | With a prefetched pack: denies the turn's first tool call and hands the recalled memories **straight to the model** via `agent_message`. Without one: falls back to gating only write-ish tools and asking for `memory_prepare` |
| `memory-mark.py` | `postToolUse` | Writes `.prepared` after prepare/ask, `.remembered` after remember |
| `memory-ensure-remember.py` | `stop` | Follow-up if nothing was recorded; clears read-side markers per turn and prunes state older than 7 days |

**Why retrieve before the agent starts.** Waiting for the agent to remember to call `memory_prepare`
fails two ways: it may skip the call, and when it does call, it searches with its own paraphrase.
Prefetch uses *your* wording and finishes before the agent does any work.

Retrieval goes through local `POST /api/prepare`, a soft-recall-only endpoint: it does not append the
"record this" suffix, create pending items, or reinforce hits — with a request per user message, any
side effect would pollute weights and hit counts. It is served by BloomBox / `app_web.py --api-only`;
when that is not running, prefetch **degrades silently** to "ask the agent to search", never erroring
and never stalling your send (1.5s timeout).

Delivery goes through `preToolUse`'s `agent_message` rather than injecting into the prompt, because:

- `beforeSubmitPrompt` **cannot modify the prompt**. Cursor staff confirmed it consumes only
  `continue` / `user_message` and supports no prompt-modifying field, and since the validator does
  not reject unknown fields, returning `additional_context` fails *silently* — the Hooks log says
  "Merged 1 valid response(s)" while the model receives nothing
- `sessionStart`'s `additional_context` is **also unreliable** (this file previously called it the
  usable injection point; that is outdated): it is applied through the composer handle, but
  `sessionStart` runs async and often finishes before that handle exists, so the value is dropped —
  most likely on the first message of a new chat
- `postToolUse`'s `additional_context` is acknowledged as not plumbed through to the model
- `preToolUse`'s `agent_message` on denial is documented *and* verified to reach the model

So the read-side rule is "interrupt only when there's something to hand over":

- Relevant memory found → deny the turn's first tool call (including `Read` / `Grep`), deliver, done.
  The same message tells the agent to **maintain** what it just received: anything contradicting
  current reality should be fixed via `memory_update` using the `id=` shown, or dropped with
  `memory_delete`. Merely noting "that one is outdated" in the reply does not count
- Nothing relevant → prefetch marks the turn satisfied, so **nothing is ever interrupted**
- Backend not running → old rule: gate only `Write` / `Delete` / `Task` and Feishu writes
- All `memory_*` tools always pass, otherwise "search memory first" would block itself

Other constraints:

- Script sources live in `cursor_hooks/` and are copied into `~/.cursor/hooks/` on install. They use
  **stdlib only**, so once installed they are fully decoupled from this repo: delete the repo or
  uninstall BloomBox and the hooks keep working (a unit test guards that constraint)
- Markers live in `~/.cursor/memory-sandbox-hook-state/` keyed by `conversation_id`, cleared at the
  start of each turn (`beforeSubmitPrompt`) and again by `stop` as a backstop, so it is "search every
  turn, record every turn"
- At most one denial per turn, so an unavailable MCP server can never deadlock the turn;
  `loop_limit: 1` keeps the stop hook to a single nudge
- Every script fails open (`{"continue": true}` / `{}` / `permission: allow`, exit 0) and can never
  block normal work
- Sub-agents run under their own fresh `conversation_id` and cannot see the parent's pack, so they
  search once themselves
- **All five scripts no-op when `MEMORY_SANDBOX_NESTED=1`.** The bots use the local agent CLI as
  their model (`CursorLocalAgentLLM` in `core/llm.py`, with `--approve-mcps`), so that nested
  process also has the memory MCP and inherits these hooks. Left alone, the `stop` gate forces it
  to record one memory and the bot then records another — **two entries per exchange**, with
  different titles and tags. The nested agent only produces text; the caller owns the write, so
  `core/llm.py` sets that variable when spawning it

Do **not** add a project-level `.cursor/hooks.json` back to this repo — having both would run twice.
The `sessionStart` injection only applies to newly opened conversations; prefetch and the read-side
gate are live as soon as the file is saved.

Two traps around global rules: Cursor does not support `~/.cursor/rules/*.mdc` (silently ignored),
and User Rules exist only in **Customize → Rules** and are not included in profile exports.

### Tags & types (easier to find)

- Tag memories (`feishu`, `frontend`, or `#tag` in text)
- Optional kinds: QA / command / path / env / pitfall / decision
- Hits include score + reasons so you can verify or forget

### Less friction, safer writes

- Extract candidates from terminal/logs, then confirm before saving
- Auto-redact tokens/secrets/private keys on write
- Web sidebar: filter by tag; edit tags/kind in the modal
- BloomBox notices memories written by *other* processes (MCP from another project, CLI) within
  seconds: a "N 条新" pill, highlighted rows, and a note in the transcript. It polls
  `GET /api/long_term_revision`, which only `stat`s the store for an `mtime:size` stamp instead of
  refetching every record
- The header 「配置」 button edits the `config.yaml` that is actually in effect (the one under
  Application Support, not the copy in the repo). Secrets such as `app_secret`,
  `user_access_token` and `llm.api_key` render as `'********'` — leave a mask alone to keep the
  value, or replace the whole mask to change it. Saving parses the text first and refuses to write
  anything it cannot load, then backs up to `config.yaml.bak-edit` and replaces the file
  atomically, because the same file holds the Feishu tokens. Config is read at process start, so
  restart BloomBox (and the bot, from its own dialog) afterwards

### Better search & sharing

- Hybrid retrieval: vectors + keywords + BM25 (Web toolbar “Retrieval settings”, with per-field help; also saved to user `config.yaml`)
- Soft-decay rarely used items; archive when needed
- Export scrubbed knowledge packs for teammates to import

### Stay in sync with code & Feishu

- `git-check`: flag memories that may be outdated after git changes
- `review-suggest`: turn recent commits into habit/convention candidates
- `feishu-bookmark`: turn Feishu docs into confirm-to-save candidates
- `pack-list`: list locally exported packs

## Knowledge base: recall whole documents, not just Q&A

Memories are one question and one answer, which suits conclusions. But a lot of team
knowledge — conventions, templates, process — lives in Feishu docs that don't compress
into a sentence. The knowledge base is a separate layer for that: documents are split
into 600–900 character chunks along their headings, each chunk gets its own vector, and
the relevant few come back as reference material when you ask something.

**Why not just store them as memories**: one vector for a several-thousand-word document
is no vector at all — it matches everything a little and nothing well — and a few hundred
chunks would bury the "saved" list in the sidebar. So it sits alongside long-term memory
(`core/knowledge.py`), shares the same embedder, and the two only meet during soft recall.

**Three ways in:**

1. The "知识库" tab in BloomBox: paste a Feishu doc link and press Enter (synchronous;
   a long doc takes a few seconds)
2. Any Feishu link inside a memory you save gets fetched into the knowledge base in the
   background — no need to add it a second time
3. **Backfill**: step 2 only fires at the moment a memory is written, so links in memories
   saved before the knowledge base existed were never fetched. `python3 main.py
   knowledge-backfill` scans every long-term memory and fills the gaps (`--dry-run` to see
   what it would fetch, `--refresh` to re-fetch stored ones too). In the UI it's the
   "从记忆补录" button in the knowledge tab (queued in the background, the list refreshes
   itself when each document lands); over MCP it's `memory_knowledge_backfill`.

Deduplication recognises both kinds of token: a wiki link's token and the docx
`document_id` Feishu resolves it to are different strings, and both are recorded on the
document, so the same doc isn't re-fetched or stored twice just because it was referenced
through a wiki link this time.

The automatic path is queued on a background thread on purpose: `fetch_feishu_document`
refreshes a token and pulls the full body, which takes seconds, while `remember` is called
synchronously by the MCP tools and the bot. A failed fetch only leaves `last_error` on the
document (flagged red in the tab) and never affects the memory write.

**How it shows up in recall**: inside the `context_pack` of `memory_prepare` / `memory_ask`,
knowledge chunks form their own section labelled as document excerpts, with the link and
section name. They are **not memory entries** — they have no memory id and cannot be passed
to `memory_update` / `memory_delete`. If an excerpt is wrong, fix the document and re-fetch.
Hard hits (`ask_local`) deliberately ignore the knowledge base: that path means "the memory
already has a settled answer", and a raw document excerpt is not that.

**One copy per document**: deduplication keys on the Feishu `document_id`, so re-adding
updates in place, and a wiki link and a docx link to the same document don't both get stored.

**Backups cover it too**: `backup` / `memory_backup` / the "备份长时" toolbar button write a
pair of files into `backups/` — `declarative_<stamp>.json` and `knowledge_<same stamp>.json` —
and `restore` finds the pair by timestamp and restores both. Older backups that predate this
feature simply leave the knowledge base untouched rather than wiping it. The snapshot stores
**no vectors**: a chunk's 256-dimension vector is several times longer than its text as JSON,
vectors are recomputed from the text anyway, and leaving them out means an old backup still
restores after the embedding dimension changes.

| Purpose | MCP tool | CLI | UI |
| --- | --- | --- | --- |
| Add a document | `memory_knowledge_add` | `knowledge-add <url>` | Input box in the knowledge tab |
| See what's stored | `memory_knowledge_list` | `knowledge-list` | Knowledge tab list |
| Backfill links from memories | `memory_knowledge_backfill` | `knowledge-backfill` | "从记忆补录" button |
| Re-fetch / remove | — | — | "重新拉取" / "删除" in the detail pane |

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
| `backup` / `restore` | Backup / restore long-term memory together with the knowledge base |
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
| `记一下 <content>` / `把 <content> 存到记忆库` / `<content>，记下来` | Write long-term memory, phrased however you like |
| `记住：问题 => 答案` | Split question and answer yourself |
| `记一下这个结论` | A bare demonstrative stores the previous exchange |
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

Replying has the same trap, only better hidden: the `comments` *response* carries a `comment_id`
documented as "reply to an existing comment if set", but the **request body spec has no such
field** — passing it is silently ignored, so every "reply" became a fresh whole-file comment at
the bottom of the document (found and fixed 2026-08-07). A reply must go to
`POST /drive/v1/files/:token/comments/:comment_id/replies` with `{"content": {"elements": [...]}}`
— not the `reply_list` shape — and it returns a `reply_id` (which is what reactions attach to)
while the thread keeps its original `comment_id`.

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
`memory_feishu_edit_body` (`mode=append`/`replace`), `memory_feishu_set_title`,
plus the whiteboard trio `memory_feishu_create_board`, `memory_feishu_board_draw` and
`memory_feishu_list_boards`.

Pick the right reader: `memory_feishu_read` when you need the **body** (returns plain text, at most
`max_chars` per call — default 30000 — with a `next_offset` to continue until it comes back `null`);
`memory_feishu_preview` when you only need to know which document and how many blocks, which
deliberately omits the body to save tokens; `memory_feishu_bookmark` to turn a document into a
pending memory candidate, where the body is truncated to roughly 1200 characters.

Not everything outside the text blocks is lost. Measured on a real 828-block design doc: the source
of `add_ons` widgets (block type 40 — mermaid sequence diagrams and the like) **already rides along
in `raw_content`**, so it needs no extra call. Whiteboards (43), images (27), spreadsheets (30),
bitables (18) and mindnotes (29) are separate resources whose block only carries a token. Of those,
whiteboards are now read: `memory_feishu_read` defaults to `include_widgets=true` and appends an
appendix rendering each board as an indented shape list plus its connectors (`A --yes--> B`). The
rest are listed with an explicit "not read, here is what's missing" line rather than vanishing
silently. Set `include_widgets=false` (CLI: `python3 main.py feishu-read <URL> --no-widgets`) to skip
the two extra requests when you only want the body.

Reading boards requires the `board:whiteboard:node:read` scope. Enable it in the Open Platform
console **before** re-running `scripts/feishu_login.py` — requesting a scope the app has not enabled
fails the whole authorization with 20027, not just that one scope.

On every write tool `confirmed` is **required and must be `true`** —
omitting it or passing `false` fails immediately without issuing a single request, the same gate the
CLI enforces interactively. After editing `mcp_server.py`, restart the MCP server; otherwise clients
keep the tool list from their last handshake.

### Creating whiteboards and drawing flowcharts

There is **no such thing as a standalone whiteboard file** in Feishu: the Open Platform docs state
that boards only have node-level APIs. A board is always a `block_type: 43` block inside some
document, and that block's `board.token` is the `whiteboard_id` (which the UI never shows). So
"create a board" means appending a board block to a document, and "draw" means posting nodes to a
`whiteboard_id`.

```bash
# New document holding a board, with a top-down flow drawn into it
python3 main.py feishu-create-board --title "Release flow" \
  --step "Open MR" --step "CI green" --step "Canary" --step "Full rollout"

# Board inside an existing document, drawn left to right
python3 main.py feishu-create-board --url <doc URL> --direction right --step A --step B

# Draw into a board that already exists (look up the invisible id first)
python3 main.py feishu-boards <doc URL>
python3 main.py feishu-board-draw <whiteboard_id> --step Alert --step Triage --step Fix
```

`--shape` takes `round_rect` (default), `rect`, `ellipse`, `diamond` or `parallelogram`, and
`--label` puts text on the connector between step *i* and *i+1*. Nodes are appended, never replacing
what is already on the canvas, up to 3000 per request.

Creating the board rides on the existing docx block API and needs no new permission, but **drawing
into it requires `board:whiteboard:node:create`**; the error message names the scope, and you then
have to re-run `scripts/feishu_login.py` because scopes are baked into the token. If the board is
created but the drawing fails, the result still carries the `whiteboard_id` — that half-finished
board has to be visible so you can clean it up. Board creation records its own long-term memory
entry (a "画板记录" question, kept separate from body and comment entries); drawing into an existing
board does not, since there is no stable document to attach it to.

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
| Changed `core/` but behaviour is unchanged | The backend is a long-lived process; code loaded at import does not hot-reload. BloomBox now swaps a stale one out automatically by comparing a `code_stamp` (content hash of `app_web.py` + `core/*.py`, computed identically in `app_web.py::compute_code_stamp` and `api_server.rs::expected_code_stamp`). Manually: `curl -X POST 127.0.0.1:8765/api/shutdown` then relaunch `python3 app_web.py --api-only`. MCP is the same — refresh it in Cursor |
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

## Feishu bot (WebSocket)

DM the bot in Feishu, or @ it in a group, to query the same long-term memory
BloomBox and the Cursor MCP use — and to write to it.

```bash
pip install lark-oapi
python3 feishu_bot.py --check   # credentials, allowlist, SDK
python3 feishu_bot.py           # foreground; Ctrl+C to quit
```

### Buttons instead of a babysat terminal

The bot is a long-running process, and keeping a terminal window open just to
host it means closing the window takes the bot down with it. So BloomBox puts a
**「飞书机器人」** button in the header next to the theme picker and 「记忆」;
the dot on it turns green while the bot is up. Opening it shows the PID, the
allowlist size and the tail of the log, with start / restart / stop buttons.
The CLI does the same thing headless:

```bash
python3 main.py bot-status    # exits 1 when it is not running, so cron can use it
python3 main.py bot-start     # background, logs to a file
python3 main.py bot-restart   # what you want after editing the config
python3 main.py bot-stop
```

Worth knowing:

- The process gets **its own session**, so quitting BloomBox or restarting the
  API server leaves the bot running. Only an explicit stop takes it down — that
  is deliberate; otherwise closing a window would silently drop the connection
- Logs land in `~/Library/Application Support/MemorySandbox/logs/feishu_bot.log`
  and are truncated past 2MB on the next start. If the bot **dies within a
  second** (missing SDK, half-filled config) the log tail comes back inline with
  the failure, so you never have to go dig for it
- Liveness is not just the pidfile: pids get recycled, so the command line is
  checked too. A bot you started yourself in a terminal is recognised as well
  (labelled as started outside BloomBox) and the stop button reaches it — two
  instances would answer every message twice, so stop takes down both

**Why WebSocket, not webhook**: the sandbox runs on your machine with no public
address. Webhook mode needs Feishu to reach a public HTTPS URL (tunnel or server);
the long connection only needs outbound internet. It is limited to custom apps.

### Setup

1. Open Platform → add the **bot** capability
2. Grant `im:message.p2p_msg:readonly`, `im:message.group_at_msg:readonly`,
   `im:message:send_as_bot`, plus the optional `im:message.reactions:write_only`
   for the progress reactions and `im:message.group_at_msg.include_bot:readonly`
   to hear **other bots** mention it (see "Letting other bots drive it"). The bot
   speaks as the app (tenant_access_token), so unlike the document scopes this
   needs **no** re-run of `feishu_login.py` — just publish a version
3. Start the bot locally **first** (`python3 feishu_bot.py`, or BloomBox →
   「飞书机器人」→ 启动)
4. **Events & callbacks → Event config** → subscribe `im.message.receive_v1` →
   choose "receive events over long connection" → save

Order matters: saving the long-connection subscription fails unless the local
process is already connected. The "Callback config" tab is for card buttons, not
for incoming messages.

### The allowlist is not optional

Anyone in the tenant can DM a bot, and your memory holds internal paths and
decisions. With an empty allowlist the bot answers nothing and only replies with
the sender's `open_id` so you can configure it:

```yaml
feishu:
  bot_allow_open_ids: ["ou_xxx"]
```

Override with `FEISHU_BOT_ALLOW=ou_a,ou_b`.

### Talking to it

| Message | Effect |
|---------|--------|
| anything | search memory: the hit plus up to 3 **other** related entries |
| `记一下 <content>` / `把 <content> 存到记忆库` / `<content>，记下来` | write to long-term memory, phrased however you like |
| `记一下：<question>`<br>`<answer…>` | split it yourself with a newline (or `=>` / `\|\|` on one line) |
| reply to a message + `记一下这个结论` | store that message as the answer |
| `状态` | counts and current scene |
| `帮助` | usage |

The entry that was just answered never shows up again under 「相关记忆」. Soft recall
runs on the same query, so the hit is always in there too, and with the highest
score — printing it verbatim turns the reply into "here is the answer, and here is
the same paragraph again". `_do_ask` therefore drops it by id (from `meta.hits`);
working- and procedural-memory hits carry no id, so it falls back to comparing text
(equal, or at least 20 characters long and contained in the answer — a short entry
that happens to be a substring is usually a different memory). If nothing is left,
the 「相关记忆：」 header is not printed at all.

Replying is how you capture what someone else (or an alert card) said: the event
only carries `parent_id`, so the bot fetches the quoted message and flattens it —
text, rich text and **interactive cards** all work. A demonstrative like
「这个结论」 is not usable as a question, so the quoted first line becomes one;
write `记一下 <your wording>` to name it yourself. Reading a quoted message in a
group needs `im:message.group_msg` on top of the scopes above — without it the bot
says so instead of storing half an entry.

**In groups the bot only answers when mentioned.** Once the group-message scope is on,
every message in the chat reaches the bot, and without a check it will butt into talk
aimed at other bots (this happened: the user mentioned an ops assistant and BloomBot
answered too). So startup calls `bot/v3/info` once to learn its own open_id and name,
then matches those against the event's `mentions`; private chats are unaffected. If the
open_id cannot be read, the bot **keeps answering everything** and warns at startup —
a group that goes silent produces no error to trace, which is worse than chattiness.

### Letting other bots drive it

Bot-sent messages do not reach you by default: both `im:message.group_at_msg:readonly` and
`im:message.group_msg` only push what *users* send, so another bot mentioning you produces
no event at all. `im:message.group_at_msg.include_bot:readonly` fixes that, and Slardar or
Mira can then mention BloomBot directly to trigger a write — no human relay needed.

Two gates still apply once the events arrive, both in `core/bot.py`:

- **We must recognise ourselves first.** Our own messages are pushed back too, and acting on
  them is an instant loop. So `parse_event` accepts a bot sender only when `self_open_id` is
  known *and* differs from it; if we cannot tell who we are, every bot message is dropped.
  Missing a message beats talking to yourself. The open_id comes from `bot/v3/info` at startup
- **Bots go through the allowlist too.** Add that bot's `open_id` to `bot_allow_open_ids` or
  it is dropped **silently** — the refusal text exists to tell a *human* their open_id, and
  shouting it at a bot just adds noise nobody reads

If you ever want messages that do *not* mention the bot, that is a different scope
(`im:message.group_msg.include_bot:read`). This project does not need it — in groups the bot
only answers when mentioned.

**Cards posted by other apps have no readable body — for anyone.** Across 22 messages in
one chat, every `interactive` message sent by an app came back as a 157–179 byte husk
(one `image_key` plus empty text), while a card sent by a user came back in full at 6541
bytes — the wall is cross-app, not cards as such.

We assumed reading as the **user** would get through, since you can see the card in your
own client. Measured on 2026-08-06, that assumption is **false**: with
`im:message:readonly` granted, the user identity returns byte-for-byte the same husk.
It is not a permission problem; Feishu simply does not hand out another app's card body.
So `fetch_quoted` now retries as the user only when the app identity **errors out**
(a missing `im:message.group_msg` and the like is still recoverable); the "retry on an
empty husk" branch was deleted because it only ever burned an extra request.

**An unreadable card does not mean unreachable context.** Other assistants answer the very
same card, but for two different reasons — this section used to claim they all merely read
the upstream message, which is false for Mira (corrected 2026-08-06):

- **Aime really did not read the card.** It replied "the image you attached is a rocket
  decoration, no text in it", i.e. it saw the same placeholder we do, and worked from the
  **message upstream** — a route we can take too.
- **Mira really did read the body.** Its write-up states three facts ("same Session", the
  `/portal/anchor/relation` page, `release 1.0.4.2376`) that appear nowhere in the upstream
  alert text but verbatim in the card we cannot read. Mira is a Feishu **first-party** app
  and does not go through `/open-apis`; no third-party app can get there, and no scope
  unlocks it (`im:chat.*` covers properties of the chat itself, never message bodies).

On the measured chat, Slardar's conclusion card is a 157-byte husk, but six messages earlier
the alert itself — forwarded into the group **by a person** — is `sender_type=user`, 6541
bytes, 1115 readable characters, handed over without complaint. On the route open to us,
`sender_type` is what decides, not the card.

So quoting another bot's card and saying 记下来 now makes the bot go get that context:
`list_chat_messages` pages backwards until it finds the quoted message, takes the dozen
before it, drops husks and "please upgrade your client" placeholders (those do yield text,
which makes them worse than empty), keeps the longest ones when over budget (alerts and
logs are the substance), and hands the result to the model. The model must answer as
`问题：` / `答案：` two lines before anything is stored — feeding the raw transcript to
`sb.chat()` would make the whole conversation the question, unsearchable and unreadable.
The whole detour only runs on a write intent with an unreadable quote and a model
configured, so ordinary messages still cost one request.

**When the upstream has nothing, forwarding does.** Another bot's analysis card — the
investigation steps, the root cause, the suggested actions — exists only inside the card
we cannot read, and the alert upstream cannot substitute for it. But the wall is
`sender_type=app`: have a **person forward** that same card and the sender becomes them
(`sender_type=user`), at which point Feishu hands over the full body — that is exactly how
the 6541-byte alert card in the group got there. So when the model comes up empty the bot
offers forwarding first and pasting second, both spelled out in the reply.

### Reactions as a progress bar

Feishu has no "bot is typing" state, and the bot may reload memory and fetch a
quoted message before it can answer. So it reacts to **your** message with `OnIt`
the moment it picks the job up, then swaps that for `DONE` once the reply is out
(`CrossMark` if sending failed). Needs `im:message.reactions:write_only`; without
it the bot logs one hint at startup and keeps answering as usual. Messages it
ignores (its own, redelivered, not allowlisted) get no reaction.

Duplicate deliveries are answered once. Pure logic lives in `core/bot.py` (no SDK
import, fully unit tested); the transport is in `feishu_bot.py`.

### A miss now goes to the agent

A local miss used to end with "not in memory", handing the question straight back
to you. It now falls through to whatever `config.llm` points at (the local Cursor
agent by default) and the answer ends with a note saying it came from the model
and was stored — only when it really was; failures like `[LLM Error]` never claim
to be saved. With no model configured the old wording stays.

That path takes tens of seconds while **a long-connection callback must return
within 3 seconds or Feishu redelivers the event**, so slow work goes onto a
single in-process worker thread: the callback only enqueues, the answer is sent
when it is ready, and the `OnIt` reaction stays up meanwhile. A full queue
(32 by default) falls back to the local search result rather than stalling.

Cap the model separately for chat — nobody waits ten minutes in a chat window:

```yaml
feishu:
  bot_llm_timeout: 150   # seconds; a shorter llm.timeout wins
```

### Mentioning it in document comments

Write `@BloomBot …` in a Feishu doc comment and it answers **in that same
thread**:

| Comment | Effect |
|---------|--------|
| `@BloomBot what is the rule here` | memory + document body → model → reply in the thread |
| anchored comment + `@BloomBot 把「三天」改成「五天」` | **proposes** the edit: "here is A → B, nothing changed yet" |
| `确认` under the proposal | applies it, then reports what changed |
| `算了` under the proposal | drops the proposal |

Boundaries hard-coded into the flow:

- **Every body edit is proposed first and only applied after you reply 确认**;
  `confirmed=True` is passed only on that post-confirmation call
- Confirmation words must be unambiguous (`确认` / `同意` / `改吧` / `ok` …).
  "确认一下这个数对不对" is **not** a yes — prefix matching there means guessing
  your way into someone's document
- Only two kinds of edit: **replacing one block** (an anchored comment carries the
  quote, so the block can be located) and **appending at the end**. No
  `mode=replace` rewrite — that deletes every existing block
- Before writing it re-reads the block and compares it with the proposal; if
  someone else changed it in the meantime the write is refused
- Cannot locate the text, matches several blocks, cannot tell what to write →
  it says why it did nothing instead of guessing
- People outside the allowlist get **silence, not an explanation** (replies are
  visible to every collaborator on the document)
- Proposals expire after 24 hours

Setup, on top of the bot above:

1. Re-run `python3 scripts/feishu_login.py` to grant the added
   `docs:event:subscribe` (`docs:document.comment:read` and
   `docs:document.comment:create` were already in the scope list)
2. **Events & callbacks → Event config** → add `drive.notice.comment_add_v1`,
   subscribing **as a user**, still over the long connection
3. Turn it on and restart:

```yaml
feishu:
  doc_bot_enabled: false         # off by default
  doc_bot_trigger: "@BloomBot"   # must appear in the comment
  doc_bot_ack_after_seconds: 8   # fallback only: post "got it, working" after 8s
```

Comments get the same **emoji progress bar** as chat: your reply is pinned with
`Typing` when the bot picks it up, switched to `CheckMark` when it is done, and to
`CrossMark` when it could not answer or the edit failed. Doc reactions hang off the
**reply_id** (not the comment id) via
`POST /drive/v2/files/{token}/comments/reaction`, which the existing
`docs:document.comment:create` scope already covers.

**Once the emoji is pinned the textual "got it" reply is skipped** — every reply in
a comment thread is visible to all collaborators, and an emoji is far quieter.
`doc_bot_ack_after_seconds` is only the fallback threshold for when the reaction
cannot be pinned. Discussion in the thread that is not aimed at the bot gets no
reaction at all.

Startup calls `POST /drive/v1/user/subscription` once (idempotent); a failure is
a warning and the IM half keeps working. `python3 feishu_bot.py --check` shows
whether the comment bot is on.

Two things to know up front:

- **Replies and reactions go out under the app identity, so the document shows
  BloomBot** rather than your name. The comment APIs (`comments`,
  `new_comments`, `comments/reaction`) accept either token and **the byline
  follows the token**: `tenant_access_token` is the bot, `user_access_token` is
  you. Resolving the document still uses your user token (wiki node lookup needs
  your permissions); only the final write goes out as the app, falling back to
  your identity when the app has no access to that document. Automated replies
  keep the `🤖 BloomBot 自动回复` prefix because it is the self-loop guard
  (`is_bot_reply` matches on it), not a stand-in for the byline. Comments you ask
  the AI to leave (`memory_feishu_comment`) still carry your name — `as_app`
  defaults to `False`, because those opinions are yours
- The event fires when **you** would be notified about a comment, so it only
  covers documents you get notifications for

Off by default on purpose: enabling it pre-authorises replies in threads where an
allowlisted person mentions the bot, while body edits still need per-change
confirmation. Pure logic lives in `core/doc_bot.py`, persisted state in
`core/doc_bot_state.py`, orchestration in `handle_comment` in `feishu_bot.py`.

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
│   ├── knowledge.py     # knowledge base: chunked document store + chunk search
│   ├── knowledge_chunk.py   # heading/paragraph chunking
│   ├── knowledge_ingest.py  # Feishu fetch into the store (sync entry + background queue)
│   ├── embedding.py     # local hash vectors (no model download)
│   ├── rules.py         # lightweight rule engine
│   ├── llm.py           # LLM adapters
│   └── sandbox.py       # main chat() orchestration
├── examples/demo.py
├── .agents/skills/      # vendored third-party skills (UI craft rules, see below)
├── skills-lock.json     # source & hash for those skills
└── data/memory/         # persistence after runs
```

## UI changes go through emilkowalski/skills

Motion and visual decisions for the desktop app and the web UI are not improvised.
[emilkowalski/skills](https://github.com/emilkowalski/skills) is vendored into
`.agents/skills/` (9 skills, real files, committed), and
`.cursor/rules/ui-craft.mdc` binds it to `desktop/src/**` and `app_web.py`: the
agent must read `.agents/skills/emil-design-eng/SKILL.md` before touching those
files, then pick `animate`, `review-animations`, `improve-animations` and friends
per task.

The same rule file records where this repo differs from the skills' assumed stack,
so nothing gets copied blindly: no framer-motion (React 19 + plain CSS only, so
springs must be done in CSS), `App.css` currently has zero `transition`
declarations (the first animation has to establish easing tokens in `:root`),
themes are `:root` plus `[data-theme]` so every new color needs both, and external
links must go through `openExternal.ts`.

To upgrade or reinstall:

```bash
npx --yes --registry=https://registry.npmjs.org skills@latest add emilkowalski/skills \
  -s '*' -a cursor --copy -y
```

`--copy` is required—the default symlinks, which cannot be committed. Files under
`.agents/skills/` are build output: to deviate from the author's advice, edit
`.cursor/rules/ui-craft.mdc` instead of the skill body.

## Design trade-offs (local-first)

1. **Embedding**: default local feature-hash vectors—no model download; reproducible matches in the same store.
2. **Vector store**: default JSON persistence, zero external services; swap to Chroma/FAISS later if needed.
3. **LLM**: pluggable; MockLLM when unset so the offline path still runs.
4. **Reinforcement**: hits raise `weight`; frequent short Q&A can settle into working-memory FAQ.
