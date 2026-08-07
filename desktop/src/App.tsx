import { useCallback, useEffect, useRef, useState } from 'react'
import {
  API_BASE,
  addKnowledge,
  archiveStale,
  backfillKnowledge,
  backupLongTerm,
  chatStream,
  clearLongTerm,
  clearWorking,
  deleteKnowledge,
  deleteMemory,
  exportPack,
  extractCandidates,
  getAgentMode,
  getCursorHooksStatus,
  getFeishuBotStatus,
  getKnowledgeDoc,
  gitCheck,
  getStatus,
  healthCheck,
  installCursorHooks,
  listKnowledge,
  listLongTerm,
  listMemory,
  openDataDir,
  refreshKnowledge,
  remember,
  seedDev,
  setAgentMode,
} from './api/client'
import type {
  ChatMessage,
  KnowledgeDoc,
  KnowledgeDocDetail,
  MemoryRecord,
} from './api/types'
import { AnswerModal, type ModalSeed } from './components/AnswerModal'
import { Chat, sourceLabel } from './components/Chat'
import { ConfigModal } from './components/ConfigModal'
import { CursorHooksModal } from './components/CursorHooksModal'
import { DialogHost } from './components/DialogHost'
import { FeishuBotModal } from './components/FeishuBotModal'
import { KnowledgeDetail } from './components/KnowledgeDetail'
import { MemoryDetail } from './components/MemoryDetail'
import { RetrievalModal } from './components/RetrievalModal'
import { SideList, type SideTab } from './components/SideList'
import { StatusBar } from './components/StatusBar'
import { alertDialog, confirmDialog } from './dialogs'
import { useMemoryWatch } from './hooks/useMemoryWatch'
import { useTheme } from './hooks/useTheme'
import { applyTheme, loadThemePreference } from './theme'
import './App.css'

applyTheme(loadThemePreference())

const PENDING_KEY = 'ms_desktop_pending_q'
const EXTRACT_KEY = 'ms_desktop_extract_map'
// 首次启动问过一次就不再打扰；用户拒绝后可在「AI 门禁」里自己开
const HOOKS_ASKED_KEY = 'ms_desktop_hooks_asked'
const BOT_POLL_MS = 20000

function loadPending(): string[] {
  try {
    return JSON.parse(localStorage.getItem(PENDING_KEY) || '[]') as string[]
  } catch {
    return []
  }
}

function loadExtractMap(): Record<string, ModalSeed & { question: string }> {
  try {
    return JSON.parse(sessionStorage.getItem(EXTRACT_KEY) || '{}') as Record<
      string,
      ModalSeed & { question: string }
    >
  } catch {
    return {}
  }
}

function uid(): string {
  return Math.random().toString(36).slice(2, 10)
}

/** 记住每条记忆的内容指纹与标题，用来分辨新增 / 被覆盖 / 被删。 */
type MemoryStamp = { stamp: string; question: string }

/**
 * 一条记忆的内容指纹。
 *
 * 只算内容字段：hit_count / weight / updated_at 每次被检索命中都会变，
 * 拿它们判断「改没改」会把别人查一次记忆也报成一堆更新。
 */
function contentStamp(rec: MemoryRecord): string {
  const src = [
    rec.question,
    rec.answer,
    rec.scene,
    rec.kind,
    (rec.tags || []).join(','),
  ].join('\u0000')
  let h = 0x811c9dc5
  for (let i = 0; i < src.length; i++) {
    h ^= src.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(36) + ':' + src.length
}

function withIds(prev: Set<string>, recs: MemoryRecord[]): Set<string> {
  if (!recs.length) return prev
  const next = new Set(prev)
  recs.forEach((r) => next.add(r.id))
  return next
}

function withoutId(prev: Set<string>, id: string): Set<string> {
  if (!prev.has(id)) return prev
  const next = new Set(prev)
  next.delete(id)
  return next
}

type InputMode = 'chat' | 'memory' | 'extract'
type AgentMode = 'ask' | 'plan' | 'agent'

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'sys',
      text:
        'BloomBox（记忆沙箱桌面端）会自动启动本机 API。\n' +
        `默认连接 ${API_BASE}\n` +
        '左侧是长时记忆标题（新记的在上，可搜索/按标签筛），点标题在这里看全文。\n' +
        '工具栏含 Agent、记忆查看、备份/清空、提炼、检索设置、AI 门禁等。',
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [apiBootHint, setApiBootHint] = useState('正在连接 / 启动本机 API…')
  const [inputMode, setInputMode] = useState<InputMode>('chat')
  const [tab, setTab] = useState<SideTab>('pending')
  const [pending, setPending] = useState<string[]>(loadPending)
  const [saved, setSaved] = useState<MemoryRecord[]>([])
  const [tagFilter, setTagFilter] = useState('')
  const [sideQuery, setSideQuery] = useState('')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [docs, setDocs] = useState<KnowledgeDoc[]>([])
  const [docDetail, setDocDetail] = useState<KnowledgeDocDetail | null>(null)
  const [docBusy, setDocBusy] = useState(false)
  // 上次见到的每条记忆的内容指纹；尚未看过的新增条目与被覆盖条目
  const knownStamps = useRef<Map<string, MemoryStamp>>(new Map())
  const [newIds, setNewIds] = useState<Set<string>>(new Set())
  const [changedIds, setChangedIds] = useState<Set<string>>(new Set())
  const [statusLine, setStatusLine] = useState('')
  const [apiOk, setApiOk] = useState<boolean | null>(null)
  const [modal, setModal] = useState<ModalSeed | null>(null)
  const [modalBusy, setModalBusy] = useState(false)
  const [retrievalOpen, setRetrievalOpen] = useState(false)
  const [hooksOpen, setHooksOpen] = useState(false)
  const [botOpen, setBotOpen] = useState(false)
  const [botRunning, setBotRunning] = useState<boolean | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
  const [agentMode, setAgentModeState] = useState<AgentMode>('ask')
  const { preference: theme, setPreference: setTheme } = useTheme()

  const persistPending = useCallback((next: string[]) => {
    setPending(next)
    localStorage.setItem(PENDING_KEY, JSON.stringify(next))
  }, [])

  const push = useCallback((m: ChatMessage) => {
    setMessages((prev) => [...prev, m])
  }, [])

  /**
   * 拉长时记忆列表，并算出这次和上次比有哪些改动。
   *
   * markChanges 只有外部写入那条路径才传 true：自己在 App 里存/删的东西
   * 不需要再提醒一遍自己。首次加载也不算改动（那是全量）。
   */
  const refreshSaved = useCallback(async (markChanges = false) => {
    try {
      const data = await listLongTerm()
      const list = data.data?.declarative || []
      const prev = knownStamps.current
      const first = prev.size === 0
      const stamps = new Map<string, MemoryStamp>()
      const added: MemoryRecord[] = []
      const changed: MemoryRecord[] = []
      list.forEach((r) => {
        const stamp = contentStamp(r)
        stamps.set(r.id, { stamp, question: r.question })
        if (first) return
        const old = prev.get(r.id)
        if (!old) added.push(r)
        else if (old.stamp !== stamp) changed.push(r)
      })
      const removed = first
        ? []
        : Array.from(prev.entries())
            .filter(([id]) => !stamps.has(id))
            .map(([, v]) => v.question)
      knownStamps.current = stamps
      setSaved(list)
      if (markChanges && (added.length || changed.length)) {
        setNewIds((p) => withIds(p, added))
        setChangedIds((p) => withIds(p, changed))
      }
      if (data.status_line) setStatusLine(data.status_line)
      setApiOk(true)
      return { added, changed, removed }
    } catch {
      setApiOk(false)
      return { added: [], changed: [], removed: [] }
    }
  }, [])

  /**
   * 外部写入（MCP / CLI）：刷新列表并在对话里留一条提示。
   *
   * 覆盖必须单独报：沙箱会把同主题（尤其带同一飞书链接）的写入合并进旧条目，
   * 条数不变，只看 id 差集的话用户会以为「根本没写进去」。
   */
  const onExternalWrite = useCallback(async () => {
    const { added, changed, removed } = await refreshSaved(true)
    const total = added.length + changed.length + removed.length
    if (!total) return
    const lines = [
      ...added.map((r) => `· 新增：${r.question}`),
      ...changed.map((r) => `· 更新（覆盖了原内容）：${r.question}`),
      ...removed.map((q) => `· 删除：${q}`),
    ]
    const head = [
      added.length ? `新增 ${added.length} 条` : '',
      changed.length ? `更新 ${changed.length} 条` : '',
      removed.length ? `删除 ${removed.length} 条` : '',
    ]
      .filter(Boolean)
      .join('、')
    push({
      id: uid(),
      role: 'meta',
      text:
        `别处改动了长时记忆：${head}\n` +
        lines.slice(0, 5).join('\n') +
        (lines.length > 5 ? `\n· 等共 ${total} 处` : ''),
    })
  }, [refreshSaved, push])

  const refreshDocs = useCallback(async () => {
    try {
      const data = await listKnowledge()
      setDocs(data.docs || [])
    } catch {
      // 知识库是增量能力，拉不到不该把整个界面判成断线
    }
  }, [])

  useMemoryWatch(() => void onExternalWrite(), apiOk === true)
  // 后台抓取是异步的，抓完得让列表自己刷出来，否则用户以为链接白贴了
  useMemoryWatch(() => void refreshDocs(), apiOk === true, 'knowledge_revision')

  /**
   * 顶栏那颗点：机器人在不在跑。
   *
   * 别人在终端里起停也要能反映出来，所以是轮询而不是只在开关弹窗时更新。
   * 后端每次查询要 ps / pgrep 一遍，所以窗口不可见时跳过，别白跑。
   */
  const refreshBotStatus = useCallback(async () => {
    try {
      setBotRunning((await getFeishuBotStatus()).running)
    } catch {
      setBotRunning(null)
    }
  }, [])

  useEffect(() => {
    if (apiOk !== true) return
    void refreshBotStatus()
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshBotStatus()
    }, BOT_POLL_MS)
    return () => window.clearInterval(timer)
  }, [apiOk, refreshBotStatus])

  /**
   * 首次启动问一次是否开启记忆门禁。
   *
   * 不静默安装：门禁会拒绝其它项目里的工具调用，悄悄改掉用户的 Cursor 行为不合适。
   * 问过一次就落地标记，之后想开只走工具栏「AI 门禁」。
   */
  const maybeOfferHooks = useCallback(async () => {
    if (localStorage.getItem(HOOKS_ASKED_KEY)) return
    let st
    try {
      st = await getCursorHooksStatus()
    } catch {
      return
    }
    if (!st.available || st.installed) return

    localStorage.setItem(HOOKS_ASKED_KEY, '1')
    const ok = await confirmDialog(
      '给 Cursor 装一组 hook，让所有项目里的 AI 都「动手前先查记忆、结束前把结论落库」？\n\n' +
        '会合并写入 ~/.cursor/hooks.json，不会动你已有的 hook；随时可以关掉。',
      { title: '开启 AI 记忆门禁？', confirmText: '开启', cancelText: '暂不' },
    )
    if (!ok) {
      push({
        id: uid(),
        role: 'meta',
        text: '未开启 AI 记忆门禁。想开随时点工具栏「AI 门禁」。',
      })
      return
    }
    try {
      const res = await installCursorHooks()
      push({ id: uid(), role: 'meta', text: res.message })
    } catch (e) {
      void alertDialog(String(e))
    }
  }, [push])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      // 等待 Rust 侧拉起 python API（最多约 12s）
      for (let i = 0; i < 24; i++) {
        if (cancelled) return
        try {
          const h = await healthCheck()
          if (h.ok) {
            setApiOk(true)
            setApiBootHint('')
            const am = await getAgentMode()
            if (
              am.agent_mode === 'ask' ||
              am.agent_mode === 'plan' ||
              am.agent_mode === 'agent'
            ) {
              setAgentModeState(am.agent_mode)
            }
            if (am.status_line) setStatusLine(am.status_line)
            await refreshSaved()
            await refreshDocs()
            if (!cancelled) await maybeOfferHooks()
            return
          }
        } catch {
          /* retry */
        }
        setApiBootHint(`正在启动本机 API… (${i + 1}/24)`)
        await new Promise((r) => setTimeout(r, 500))
      }
      if (!cancelled) {
        setApiOk(false)
        setApiBootHint(
          '未能连接 API。请确认已安装 Python3 与依赖（pip install -r requirements.txt），或设置 BLOOMBOX_PYTHON / BLOOMBOX_API_ROOT。',
        )
        push({
          id: uid(),
          role: 'meta',
          text:
            'API 未就绪。BloomBox 会尝试自动执行 python3 app_web.py --api-only；若失败请检查 Python 环境。',
        })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [refreshSaved, refreshDocs, push])

  const openPending = (q: string, index: number) => {
    const hit = saved.find((r) => r.question === q)
    const seed = loadExtractMap()[q]
    setModal({
      question: q,
      answer: seed?.answer || '',
      pendingIndex: index,
      id: hit?.id || seed?.id,
      tags: seed?.tags,
      kind: seed?.kind,
      updateOnly: !!hit?.id,
    })
  }

  /**
   * 详情按 id 从 saved 里取，不存快照：
   * 编辑保存后会自动跟着刷新，记忆被删掉时也会自动退回对话。
   */
  const detail = detailId ? saved.find((r) => r.id === detailId) || null : null

  const openDoc = async (doc: KnowledgeDoc) => {
    setDetailId(null)
    try {
      const data = await getKnowledgeDoc(doc.id)
      if (data.doc) setDocDetail(data.doc)
    } catch (e) {
      void alertDialog(String(e))
    }
  }

  const addDoc = async (url: string) => {
    setDocBusy(true)
    try {
      const data = await addKnowledge(url)
      await refreshDocs()
      if (!data.ok) {
        void alertDialog(data.error || '录入失败')
        return
      }
      const doc = data.doc
      push({
        id: uid(),
        role: 'sys',
        text: data.skipped
          ? `知识库里已有《${doc?.title}》，跳过重复抓取`
          : `已录入知识库：《${doc?.title}》（${doc?.char_count ?? 0} 字 / ${doc?.chunk_count ?? 0} 块）`,
      })
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setDocBusy(false)
    }
  }

  const backfillDocs = async () => {
    try {
      const data = await backfillKnowledge()
      if (!data.ok) {
        void alertDialog(data.error || '补录失败')
        return
      }
      push({
        id: uid(),
        role: 'sys',
        text: data.queued
          ? `已排队补录 ${data.queued} 篇（记忆里出现过、还没入库的飞书文档）。后台抓取中，抓完列表会自动刷新。`
          : '记忆里的飞书文档都已在知识库中，没有要补录的。',
      })
    } catch (e) {
      void alertDialog(String(e))
    }
  }

  const refreshDoc = async (doc: KnowledgeDoc) => {
    setDocBusy(true)
    try {
      const data = await refreshKnowledge(doc.id)
      await refreshDocs()
      if (!data.ok) {
        void alertDialog(data.error || '重新拉取失败')
        return
      }
      // 详情是快照，重拉后要换成新的，否则看到的还是旧正文
      const fresh = await getKnowledgeDoc(doc.id)
      if (fresh.doc) setDocDetail(fresh.doc)
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setDocBusy(false)
    }
  }

  const deleteDoc = async (doc: KnowledgeDoc) => {
    const sure = await confirmDialog(`确定把《${doc.title || doc.url}》移出知识库？`, {
      title: '移出知识库',
      confirmText: '删除',
      danger: true,
    })
    if (!sure) return
    try {
      const data = await deleteKnowledge(doc.id)
      if (!data.ok) {
        void alertDialog(data.error || '删除失败')
        return
      }
      if (docDetail?.id === doc.id) setDocDetail(null)
      await refreshDocs()
      push({ id: uid(), role: 'sys', text: `已移出知识库：${doc.title || doc.url}` })
    } catch (e) {
      void alertDialog(String(e))
    }
  }

  const editSaved = (rec: MemoryRecord) => {
    setModal({
      question: rec.question,
      answer: rec.answer,
      pendingIndex: -1,
      id: rec.id,
      tags: rec.tags,
      kind: rec.kind,
      updateOnly: true,
    })
  }

  const deleteSaved = async (rec: MemoryRecord) => {
    const sure = await confirmDialog(`确定删除已记住的「${rec.question}」？`, {
      title: '删除记忆',
      confirmText: '删除',
      danger: true,
    })
    if (!sure) return
    try {
      const data = await deleteMemory(rec.id, rec.question)
      if (data.error) {
        void alertDialog(data.error)
        return
      }
      if (data.status_line) setStatusLine(data.status_line)
      await refreshSaved()
      push({ id: uid(), role: 'sys', text: `已删除：${rec.question}` })
    } catch (e) {
      void alertDialog(String(e))
    }
  }

  const onConfirmRemember = async (payload: {
    question: string
    answer: string
    originalQuestion: string
    id?: string
    updateOnly: boolean
    tags: string[]
    kind: string
  }) => {
    setModalBusy(true)
    try {
      const data = await remember({
        question: payload.question,
        answer: payload.answer,
        scene: 'dev',
        tags: payload.tags,
        kind: payload.kind,
        id: payload.id,
        original_question: payload.originalQuestion,
        update_only: payload.updateOnly,
      })
      if (data.error) {
        void alertDialog(data.error)
        return
      }
      const seed = modal
      if (seed && seed.pendingIndex >= 0) {
        const next = [...pending]
        next.splice(seed.pendingIndex, 1)
        persistPending(next)
      } else {
        persistPending(
          pending.filter(
            (q) => q !== payload.question && q !== payload.originalQuestion,
          ),
        )
      }
      setModal(null)
      push({
        id: uid(),
        role: 'sys',
        text: `${data.updated ? '已更新：' : '已记住：'}${data.stored_question || payload.question}`,
      })
      push({ id: uid(), role: 'bot', text: payload.answer, label: '答' })
      setTab('saved')
      if (data.status_line) setStatusLine(data.status_line)
      await refreshSaved()
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setModalBusy(false)
    }
  }

  const runExtract = async (text: string) => {
    push({ id: uid(), role: 'user', text: text.slice(0, 400) + (text.length > 400 ? '…' : '') })
    setBusy(true)
    try {
      const data = await extractCandidates(text)
      const cands = data.candidates || []
      if (!cands.length) {
        push({
          id: uid(),
          role: 'sys',
          text: '未提炼出候选记忆，可换一段包含命令/路径/报错的文本再试。',
        })
        return
      }
      push({
        id: uid(),
        role: 'sys',
        text: `提炼到 ${cands.length} 条候选（点击左侧填写答案）：`,
      })
      const nextPending = [...pending]
      const map = loadExtractMap()
      cands.forEach((c, i) => {
        const q = c.question || `候选${i + 1}`
        push({
          id: uid(),
          role: 'bot',
          text: `[${i + 1}] ${c.kind || 'qa'} · ${q}\n${c.answer || ''}`,
          label: '候选',
        })
        if (!nextPending.includes(q)) nextPending.unshift(q)
        map[q] = {
          question: q,
          answer: c.answer || '',
          pendingIndex: 0,
          tags: c.tags,
          kind: c.kind,
        }
      })
      try {
        sessionStorage.setItem(EXTRACT_KEY, JSON.stringify(map))
      } catch {
        /* ignore */
      }
      persistPending(nextPending)
      setTab('pending')
      if (data.suggested_tags?.length) {
        push({
          id: uid(),
          role: 'meta',
          text: '建议标签：' + data.suggested_tags.map((t) => '#' + t).join(' '),
        })
      }
      if (data.status_line) setStatusLine(data.status_line)
      setApiOk(true)
    } catch (e) {
      setApiOk(false)
      push({ id: uid(), role: 'meta', text: `提炼失败：${e}` })
    } finally {
      setBusy(false)
      setInputMode('chat')
    }
  }

  const sendChat = async (text: string) => {
    push({ id: uid(), role: 'user', text: `你：${text}` })
    setBusy(true)
    const thinkId = uid()
    push({ id: thinkId, role: 'think', steps: [] })
    try {
      await chatStream(text, (ev) => {
        if (ev.type === 'progress') {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === thinkId && m.role === 'think'
                ? { ...m, steps: [...m.steps, ev.message] }
                : m,
            ),
          )
          return
        }
        setMessages((prev) =>
          prev.map((m) =>
            m.id === thinkId && m.role === 'think'
              ? {
                  ...m,
                  done: ev.type === 'done',
                  error: ev.type === 'error',
                }
              : m,
          ),
        )
        if (ev.type === 'error') {
          push({ id: uid(), role: 'meta', text: `错误：${ev.error}` })
          return
        }
        push({
          id: uid(),
          role: 'bot',
          text: ev.answer || '',
          label: '沙箱',
        })
        push({
          id: uid(),
          role: 'meta',
          text: `← 来源：${sourceLabel(ev.source)} (${ev.source})`,
        })
        if (ev.status_line) setStatusLine(ev.status_line)
        if (ev.awaiting_confirm) {
          const pq = (ev.pending_question || text || '').trim()
          if (pq) {
            setPending((prev) => {
              const next = prev.includes(pq) ? prev : [pq, ...prev]
              localStorage.setItem(PENDING_KEY, JSON.stringify(next))
              return next
            })
            setTab('pending')
            push({
              id: uid(),
              role: 'sys',
              text: '飞书文档未自动入库。已放入左侧「待补全答」——可改「问」后确认记住。',
            })
            setModal({
              question: pq,
              answer: ev.pending_answer || ev.answer || '',
              pendingIndex: 0,
              tags: ev.pending_tags,
              kind: ev.pending_kind,
            })
          }
        } else if (ev.source === 'llm' || ev.source === 'command') {
          // 指令都可能改库：「记一下…」「忘记…」「清空…」，列表得跟着动
          void refreshSaved()
        }
      })
      setApiOk(true)
    } catch (e) {
      setApiOk(false)
      setMessages((prev) =>
        prev.map((m) =>
          m.id === thinkId && m.role === 'think'
            ? { ...m, error: true, done: false }
            : m,
        ),
      )
      push({ id: uid(), role: 'meta', text: `请求失败：${e}` })
    } finally {
      setBusy(false)
    }
  }

  const onSend = async () => {
    const text = input.trim()
    if (!text || busy) return
    setInput('')
    if (inputMode === 'memory') {
      if (pending.includes(text)) {
        push({ id: uid(), role: 'sys', text: '该问题已在待补全列表中。' })
      } else {
        persistPending([text, ...pending])
        setTab('pending')
        push({
          id: uid(),
          role: 'sys',
          text: `已加入左侧「问」：${text}\n请在左侧点选该问题，弹窗中填写「答」。`,
        })
      }
      setInputMode('chat')
      return
    }
    if (inputMode === 'extract') {
      await runExtract(text)
      return
    }
    await sendChat(text)
  }

  const onToolAction = async (id: string) => {
    // 详情顶掉了对话区，这些操作的输出都打在对话里，先退回去才看得见
    if (id !== 'retrieval' && id !== 'hooks') setDetailId(null)
    if (id === 'extract') {
      setInputMode('extract')
      push({
        id: uid(),
        role: 'sys',
        text: '已进入「提炼候选」：粘贴终端/日志文本到下方发送。',
      })
      return
    }
    if (id === 'retrieval') {
      setRetrievalOpen(true)
      return
    }
    if (id === 'hooks') {
      setHooksOpen(true)
      return
    }
    setBusy(true)
    try {
      if (id === 'working' || id === 'long' || id === 'all') {
        const layer = id === 'working' ? 'working' : id === 'long' ? 'long_term' : 'all'
        const data = await listMemory(layer)
        push({ id: uid(), role: 'meta', text: data.text || '(空)' })
        if (data.status_line) setStatusLine(data.status_line)
        if (layer === 'long_term' && data.data?.declarative) {
          setSaved(data.data.declarative)
          setTab('saved')
        }
      } else if (id === 'status') {
        const data = await getStatus()
        push({
          id: uid(),
          role: 'meta',
          text: JSON.stringify(data.status, null, 2),
        })
        if (data.status_line) setStatusLine(data.status_line)
      } else if (id === 'clear_w') {
        const data = await clearWorking()
        push({ id: uid(), role: 'sys', text: data.message || '工作记忆已清空' })
        if (data.status_line) setStatusLine(data.status_line)
      } else if (id === 'backup') {
        const data = await backupLongTerm()
        push({ id: uid(), role: 'sys', text: data.message || '已备份' })
        if (data.backups?.length) {
          push({
            id: uid(),
            role: 'meta',
            text: '最近备份：\n' + data.backups.slice(0, 5).join('\n'),
          })
        }
        if (data.status_line) setStatusLine(data.status_line)
      } else if (id === 'export') {
        const data = await exportPack()
        push({
          id: uid(),
          role: 'sys',
          text: data.message || (data.path ? `已导出：${data.path}` : '已导出'),
        })
        if (data.status_line) setStatusLine(data.status_line)
      } else if (id === 'git') {
        const data = await gitCheck()
        push({
          id: uid(),
          role: 'sys',
          text: data.hint || data.message || data.text || '已检查 Git 变更',
        })
        if (data.status_line) setStatusLine(data.status_line)
      } else if (id === 'archive') {
        if (!(await confirmDialog('确认归档久未命中的长时记忆？', { title: '归档记忆' }))) return
        const data = await archiveStale()
        push({ id: uid(), role: 'sys', text: data.message || '已归档' })
        if (data.status_line) setStatusLine(data.status_line)
        await refreshSaved()
      } else if (id === 'seed') {
        const data = await seedDev()
        push({ id: uid(), role: 'sys', text: data.message || '已写入种子' })
        if (data.status_line) setStatusLine(data.status_line)
        await refreshSaved()
        setTab('saved')
      } else if (id === 'data') {
        const data = await openDataDir()
        push({ id: uid(), role: 'sys', text: data.message || '已打开数据目录' })
      } else if (id === 'clear_l') {
        const sure = await confirmDialog('确定清空全部长时记忆？此操作不可撤销。', {
          title: '清空长时记忆',
          confirmText: '清空',
          danger: true,
        })
        if (!sure) return
        const backupFirst = await confirmDialog('清空前先备份长时记忆？', {
          title: '先备份吗',
          confirmText: '先备份',
          cancelText: '不备份',
        })
        const data = await clearLongTerm({ confirm: true, backup_first: backupFirst })
        push({ id: uid(), role: 'sys', text: data.message || '长时记忆已清空' })
        if (data.status_line) setStatusLine(data.status_line)
        persistPending([])
        await refreshSaved()
      }
      setApiOk(true)
    } catch (e) {
      setApiOk(false)
      push({ id: uid(), role: 'meta', text: `操作失败：${e}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <SideList
        tab={tab}
        pending={pending}
        saved={saved}
        docs={docs}
        tagFilter={tagFilter}
        query={sideQuery}
        activeId={detailId || docDetail?.id || modal?.id}
        newIds={newIds}
        changedIds={changedIds}
        addingDoc={docBusy}
        onSeenAll={() => {
          setNewIds(new Set())
          setChangedIds(new Set())
        }}
        onQuery={setSideQuery}
        onTagFilter={setTagFilter}
        onTab={setTab}
        onOpenPending={openPending}
        onOpenSaved={(rec) => {
          setDetailId(rec.id)
          setDocDetail(null)
          // 看过就不再标记
          setNewIds((prev) => withoutId(prev, rec.id))
          setChangedIds((prev) => withoutId(prev, rec.id))
        }}
        onDeletePending={async (idx) => {
          const sure = await confirmDialog('删除这条待补全的问题？', {
            title: '删除待补全',
            confirmText: '删除',
            danger: true,
          })
          if (!sure) return
          const next = [...pending]
          next.splice(idx, 1)
          persistPending(next)
        }}
        onDeleteSaved={deleteSaved}
        onAddDoc={addDoc}
        onOpenDoc={(doc) => void openDoc(doc)}
        onDeleteDoc={deleteDoc}
        onBackfillDocs={backfillDocs}
      />
      <div className="main-col">
        <Chat
          messages={messages}
          busy={busy}
          inputMode={inputMode}
          input={input}
          onInput={setInput}
          onSend={() => void onSend()}
          onSetMode={setInputMode}
          theme={theme}
          onThemeChange={setTheme}
          agentMode={agentMode}
          onAgentMode={(m) => {
            setAgentModeState(m)
            void setAgentMode(m)
              .then((data) => {
                if (data.status_line) setStatusLine(data.status_line)
                if (data.message) push({ id: uid(), role: 'sys', text: data.message })
              })
              .catch((e) => void alertDialog(String(e)))
          }}
          onToolAction={(id) => void onToolAction(id)}
          botRunning={botRunning}
          onBot={() => setBotOpen(true)}
          onConfig={() => setConfigOpen(true)}
          detail={
            detail ? (
              <MemoryDetail
                rec={detail}
                onClose={() => setDetailId(null)}
                onEdit={() => editSaved(detail)}
                onDelete={() => void deleteSaved(detail)}
              />
            ) : docDetail ? (
              <KnowledgeDetail
                doc={docDetail}
                busy={docBusy}
                onClose={() => setDocDetail(null)}
                onRefresh={() => void refreshDoc(docDetail)}
                onDelete={() => void deleteDoc(docDetail)}
              />
            ) : undefined
          }
        />
        <StatusBar
          line={apiBootHint || statusLine}
          apiOk={apiOk}
          apiHint={apiBootHint || undefined}
        />
      </div>
      <AnswerModal
        open={!!modal}
        seed={modal}
        busy={modalBusy}
        onClose={() => setModal(null)}
        onConfirm={(p) => void onConfirmRemember(p)}
      />
      <CursorHooksModal open={hooksOpen} onClose={() => setHooksOpen(false)} />
      <FeishuBotModal
        open={botOpen}
        onClose={() => setBotOpen(false)}
        onChanged={(st) => setBotRunning(st.running)}
      />
      <ConfigModal
        open={configOpen}
        onClose={() => setConfigOpen(false)}
        onSaved={(message) => push({ id: uid(), role: 'sys', text: message })}
      />
      <RetrievalModal
        open={retrievalOpen}
        onClose={() => setRetrievalOpen(false)}
        onSaved={(line) => {
          if (line) setStatusLine(line)
          push({ id: uid(), role: 'sys', text: '检索设置已保存' })
        }}
      />
      <DialogHost />
    </div>
  )
}
