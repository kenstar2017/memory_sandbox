import type {
  ChatStreamEvent,
  HealthResult,
  KnowledgeDoc,
  KnowledgeDocDetail,
  KnowledgeStats,
  MemoryRecord,
  RememberPayload,
  RememberResult,
} from './types'

export const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') ||
  'http://127.0.0.1:8765'

async function parseJson<T>(res: Response): Promise<T> {
  const data = (await res.json()) as T & { error?: string }
  if (!res.ok) {
    throw new Error((data as { error?: string }).error || `HTTP ${res.status}`)
  }
  return data
}

/**
 * WKWebView 把「连不上」和「被 CORS 拦掉」都报成 TypeError: Load failed，
 * 光看这句话没法排查，所以补一句能照着做的说明。
 */
async function fetchApi(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_BASE}${path}`, init)
  } catch (e) {
    throw new Error(
      `连不上后端 ${API_BASE}（${(e as Error).message}）。` +
        'Python API 可能没在跑：重启 BloomBox，或在仓库根执行 python3 app_web.py --api-only',
    )
  }
}

export async function healthCheck(): Promise<HealthResult> {
  const res = await fetchApi('/api/health')
  return parseJson<HealthResult>(res)
}

export async function apiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetchApi(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parseJson<T>(res)
}

export async function listMemory(layer: string): Promise<{
  text?: string
  data?: { declarative?: MemoryRecord[] }
  status_line?: string
}> {
  return apiPost('/api/list_memory', { layer })
}

export async function listLongTerm(): Promise<{
  data?: { declarative?: MemoryRecord[] }
  status_line?: string
}> {
  return listMemory('long_term')
}

export async function listKnowledge(): Promise<{
  docs?: KnowledgeDoc[]
  stats?: KnowledgeStats
  status_line?: string
}> {
  return apiPost('/api/knowledge/list')
}

/** 抓一篇飞书文档入库。同步接口，长文档要等几秒 */
export async function addKnowledge(
  url: string,
): Promise<{ ok?: boolean; error?: string; skipped?: boolean; doc?: KnowledgeDoc }> {
  return apiPost('/api/knowledge/add', { url })
}

/** 扫全部记忆里的飞书链接补录。抓取在后台跑，这里只回排了多少篇 */
export async function backfillKnowledge(
  refresh = false,
): Promise<{ ok?: boolean; error?: string; candidates?: number; queued?: number }> {
  return apiPost('/api/knowledge/backfill', { refresh })
}

export async function getKnowledgeDoc(id: string): Promise<{ doc?: KnowledgeDocDetail }> {
  return apiPost('/api/knowledge/get', { id })
}

export async function refreshKnowledge(
  id: string,
): Promise<{ ok?: boolean; error?: string; doc?: KnowledgeDoc }> {
  return apiPost('/api/knowledge/refresh', { id })
}

export async function deleteKnowledge(id: string): Promise<{ ok?: boolean; error?: string }> {
  return apiPost('/api/knowledge/delete', { id })
}

export async function remember(payload: RememberPayload): Promise<RememberResult> {
  return apiPost<RememberResult>('/api/remember', payload as unknown as Record<string, unknown>)
}

export async function deleteMemory(
  id: string,
  question: string,
): Promise<{ error?: string; status_line?: string }> {
  return apiPost('/api/delete_memory', { id, question })
}

export async function getStatus(): Promise<{ status?: unknown; status_line?: string }> {
  return apiPost('/api/status')
}

export async function getAgentMode(): Promise<{
  agent_mode?: string
  agent_force?: boolean
  status_line?: string
}> {
  return apiPost('/api/agent_mode', {})
}

export async function setAgentMode(mode: string): Promise<{
  message?: string
  agent_mode?: string
  status_line?: string
}> {
  return apiPost('/api/agent_mode', { mode, persist: true })
}

export async function clearWorking(): Promise<{ message?: string; status_line?: string }> {
  return apiPost('/api/clear_working')
}

export async function backupLongTerm(): Promise<{
  message?: string
  backups?: string[]
  status_line?: string
}> {
  return apiPost('/api/backup_long_term')
}

export async function clearLongTerm(opts: {
  confirm: boolean
  backup_first?: boolean
}): Promise<{ message?: string; status_line?: string }> {
  return apiPost('/api/clear_long_term', opts)
}

export async function seedDev(): Promise<{ message?: string; status_line?: string }> {
  return apiPost('/api/seed')
}

export async function openDataDir(): Promise<{ message?: string; path?: string }> {
  return apiPost('/api/open_data')
}

export async function gitCheck(): Promise<{
  hint?: string
  message?: string
  status_line?: string
  text?: string
}> {
  return apiPost('/api/git_check', {})
}

export async function archiveStale(): Promise<{ message?: string; status_line?: string }> {
  return apiPost('/api/archive', { confirm: true })
}

export async function exportPack(): Promise<{ message?: string; path?: string; status_line?: string }> {
  return apiPost('/api/export_pack', {})
}

export async function extractCandidates(text: string, max_n = 3): Promise<{
  candidates?: Array<{
    question?: string
    answer?: string
    tags?: string[]
    kind?: string
    facts?: Record<string, string>
  }>
  suggested_tags?: string[]
  error?: string
  status_line?: string
}> {
  return apiPost('/api/extract', { text, max_n })
}

export async function suggestQuestion(
  question: string,
  answer: string,
): Promise<{
  question?: string
  changed?: boolean
  hint?: string
  tags?: string[]
  error?: string
}> {
  return apiPost('/api/suggest_question', { question, answer, fetch: true })
}

export type CursorHooksStatus = {
  installed: boolean
  up_to_date: boolean
  hooks_json: string
  hooks_dir: string
  python: string
  installed_at: string
  missing_scripts: string[]
  stale_scripts: string[]
  missing_events: string[]
  foreign_entries: number
  available: boolean
  error: string
}

export type CursorHooksResult = {
  ok: boolean
  action: string
  hooks_json: string
  python: string
  events: string[]
  backup: string
  kept_foreign: number
  message: string
  error: string
}

/** 长时记忆文件的变更标记（mtime:size），用于轮询别的进程有没有写入。 */
export async function getLongTermRevision(): Promise<{
  revision: string
  knowledge_revision?: string
}> {
  const res = await fetchApi('/api/long_term_revision')
  return parseJson<{ revision: string; knowledge_revision?: string }>(res)
}

export async function getCursorHooksStatus(): Promise<CursorHooksStatus> {
  const res = await fetchApi('/api/cursor_hooks/status')
  return parseJson<CursorHooksStatus>(res)
}

export async function installCursorHooks(): Promise<CursorHooksResult> {
  return apiPost('/api/cursor_hooks/install')
}

export async function uninstallCursorHooks(): Promise<CursorHooksResult> {
  return apiPost('/api/cursor_hooks/uninstall')
}

export type FeishuBotStatus = {
  running: boolean
  pid: number
  /** true = BloomBox 起的；false = 在别处（比如终端里）起的，扫出来的 */
  owned: boolean
  started_at: string
  available: boolean
  sdk_installed: boolean
  configured: boolean
  allow_count: number
  doc_bot_enabled: boolean
  script: string
  python: string
  log: string
  log_tail: string
  error: string
}

export type FeishuBotResult = {
  ok: boolean
  message: string
  status: FeishuBotStatus
}

export async function getFeishuBotStatus(): Promise<FeishuBotStatus> {
  const res = await fetchApi('/api/feishu_bot/status')
  return parseJson<FeishuBotStatus>(res)
}

export async function startFeishuBot(): Promise<FeishuBotResult> {
  return apiPost('/api/feishu_bot/start')
}

export async function stopFeishuBot(): Promise<FeishuBotResult> {
  return apiPost('/api/feishu_bot/stop')
}

export async function restartFeishuBot(): Promise<FeishuBotResult> {
  return apiPost('/api/feishu_bot/restart')
}

export type ConfigView = {
  path: string
  /** 密钥类字段的值已被换成 mask，原样存回去即可保持不变 */
  text: string
  exists: boolean
  masked: string[]
  mask: string
  error: string
}

export type ConfigSaveResult = {
  ok: boolean
  message: string
  path: string
  backup: string
  error: string
}

export async function getConfigFile(): Promise<ConfigView> {
  const res = await fetchApi('/api/config')
  return parseJson<ConfigView>(res)
}

export async function saveConfigFile(text: string): Promise<ConfigSaveResult> {
  return apiPost('/api/config/save', { text })
}

export async function getRetrievalSettings(): Promise<Record<string, unknown>> {
  return apiPost('/api/retrieval_settings', {})
}

export async function setRetrievalSettings(
  updates: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return apiPost('/api/retrieval_settings', { updates, persist: true })
}

/** NDJSON chat stream — callbacks for progress / done / error. */
export async function chatStream(
  text: string,
  onEvent: (ev: ChatStreamEvent) => void,
): Promise<void> {
  const res = await fetchApi('/api/chat_stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!res.ok || !res.body) {
    throw new Error(`流式接口不可用（HTTP ${res.status}）`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 1)
      if (!line) continue
      try {
        onEvent(JSON.parse(line) as ChatStreamEvent)
      } catch {
        /* skip bad line */
      }
    }
  }
  if (buf.trim()) {
    try {
      onEvent(JSON.parse(buf.trim()) as ChatStreamEvent)
    } catch {
      /* ignore */
    }
  }
}
