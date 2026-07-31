import type {
  ChatStreamEvent,
  HealthResult,
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

export async function healthCheck(): Promise<HealthResult> {
  const res = await fetch(`${API_BASE}/api/health`)
  return parseJson<HealthResult>(res)
}

export async function apiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
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
  const res = await fetch(`${API_BASE}/api/chat_stream`, {
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
