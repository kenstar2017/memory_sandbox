export type ChatStreamProgress = {
  type: 'progress'
  message: string
}

export type ChatStreamDone = {
  type: 'done'
  answer: string
  source: string
  status_line?: string
  awaiting_confirm?: boolean
  pending_question?: string
  pending_answer?: string
  pending_tags?: string[]
  pending_facts?: Record<string, string>
  pending_kind?: string
}

export type ChatStreamError = {
  type: 'error'
  error: string
}

export type ChatStreamEvent = ChatStreamProgress | ChatStreamDone | ChatStreamError

export type MemoryRecord = {
  id: string
  question: string
  answer: string
  scene?: string
  tags?: string[]
  kind?: string
  facts?: Record<string, string>
  weight?: number
  hit_count?: number
  /** 秒级时间戳。created_at 决定列表顺序，updated_at 会被检索命中刷新 */
  created_at?: number
  updated_at?: number
}

/** 知识库里的一篇文档。正文不在这里，要 getKnowledgeDoc 才带 chunks */
export type KnowledgeDoc = {
  id: string
  url: string
  title: string
  document_id?: string
  source?: string
  /** manual = 手动录入；memory:<记忆id> = 跟着某条记忆里的链接自动抓的 */
  origin?: string
  scene?: string
  tags?: string[]
  char_count?: number
  chunk_count?: number
  fetched_at?: number
  updated_at?: number
  /** 非空表示上次抓取失败，列表里要标出来 */
  last_error?: string
}

export type KnowledgeChunk = {
  id: string
  seq: number
  heading_path: string
  text: string
}

export type KnowledgeDocDetail = KnowledgeDoc & { chunks: KnowledgeChunk[] }

export type KnowledgeStats = {
  doc_count: number
  chunk_count: number
  failed_count: number
  persist_dir?: string
}

export type RememberPayload = {
  question: string
  answer: string
  scene?: string
  tags?: string[]
  kind?: string
  facts?: Record<string, string>
  id?: string
  original_question?: string
  update_only?: boolean
}

export type RememberResult = {
  message?: string
  stored_question?: string
  id?: string
  updated?: boolean
  status_line?: string
  error?: string
}

export type HealthResult = {
  ok: boolean
  app?: string
  build?: string
  features?: string[]
  api_only?: boolean
}

export type ChatMessage =
  | { id: string; role: 'user'; text: string }
  | { id: string; role: 'bot'; text: string; label?: string }
  | { id: string; role: 'sys'; text: string }
  | { id: string; role: 'meta'; text: string }
  | { id: string; role: 'think'; steps: string[]; done?: boolean; error?: boolean }
