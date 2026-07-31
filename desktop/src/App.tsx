import { useCallback, useEffect, useState } from 'react'
import {
  API_BASE,
  archiveStale,
  backupLongTerm,
  chatStream,
  clearLongTerm,
  clearWorking,
  deleteMemory,
  exportPack,
  extractCandidates,
  getAgentMode,
  gitCheck,
  getStatus,
  healthCheck,
  listLongTerm,
  listMemory,
  openDataDir,
  remember,
  seedDev,
  setAgentMode,
} from './api/client'
import type { ChatMessage, MemoryRecord } from './api/types'
import { AnswerModal, type ModalSeed } from './components/AnswerModal'
import { Chat, sourceLabel } from './components/Chat'
import { RetrievalModal } from './components/RetrievalModal'
import { SideList } from './components/SideList'
import { StatusBar } from './components/StatusBar'
import { useTheme } from './hooks/useTheme'
import { applyTheme, loadThemePreference } from './theme'
import './App.css'

applyTheme(loadThemePreference())

const PENDING_KEY = 'ms_desktop_pending_q'
const EXTRACT_KEY = 'ms_desktop_extract_map'

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
        '工具栏含标签筛选、Agent、记忆查看、备份/清空、提炼、检索设置等。',
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [apiBootHint, setApiBootHint] = useState('正在连接 / 启动本机 API…')
  const [inputMode, setInputMode] = useState<InputMode>('chat')
  const [tab, setTab] = useState<'pending' | 'saved'>('pending')
  const [pending, setPending] = useState<string[]>(loadPending)
  const [saved, setSaved] = useState<MemoryRecord[]>([])
  const [tagFilter, setTagFilter] = useState('')
  const [statusLine, setStatusLine] = useState('')
  const [apiOk, setApiOk] = useState<boolean | null>(null)
  const [modal, setModal] = useState<ModalSeed | null>(null)
  const [modalBusy, setModalBusy] = useState(false)
  const [retrievalOpen, setRetrievalOpen] = useState(false)
  const [agentMode, setAgentModeState] = useState<AgentMode>('ask')
  const { preference: theme, setPreference: setTheme } = useTheme()

  const persistPending = useCallback((next: string[]) => {
    setPending(next)
    localStorage.setItem(PENDING_KEY, JSON.stringify(next))
  }, [])

  const push = useCallback((m: ChatMessage) => {
    setMessages((prev) => [...prev, m])
  }, [])

  const refreshSaved = useCallback(async () => {
    try {
      const data = await listLongTerm()
      setSaved(data.data?.declarative || [])
      if (data.status_line) setStatusLine(data.status_line)
      setApiOk(true)
    } catch {
      setApiOk(false)
    }
  }, [])

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
  }, [refreshSaved, push])

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

  const openSaved = (rec: MemoryRecord) => {
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
        alert(data.error)
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
      alert(String(e))
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
        } else if (ev.source === 'llm') {
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
        if (!confirm('确认归档久未命中的长时记忆？')) return
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
        if (!confirm('确定清空全部长时记忆？建议勾选先备份。')) return
        const backupFirst = confirm('清空前先备份长时记忆？（推荐：确定=备份）')
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
        tagFilter={tagFilter}
        onTagFilter={setTagFilter}
        onTab={setTab}
        onOpenPending={openPending}
        onOpenSaved={openSaved}
        onDeletePending={(idx) => {
          if (!confirm('删除这条待补全的问题？')) return
          const next = [...pending]
          next.splice(idx, 1)
          persistPending(next)
        }}
        onDeleteSaved={async (rec) => {
          if (!confirm(`确定删除已记住的「${rec.question}」？`)) return
          try {
            const data = await deleteMemory(rec.id, rec.question)
            if (data.error) {
              alert(data.error)
              return
            }
            if (data.status_line) setStatusLine(data.status_line)
            await refreshSaved()
            push({ id: uid(), role: 'sys', text: `已删除：${rec.question}` })
          } catch (e) {
            alert(String(e))
          }
        }}
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
              .catch((e) => alert(String(e)))
          }}
          onToolAction={(id) => void onToolAction(id)}
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
      <RetrievalModal
        open={retrievalOpen}
        onClose={() => setRetrievalOpen(false)}
        onSaved={(line) => {
          if (line) setStatusLine(line)
          push({ id: uid(), role: 'sys', text: '检索设置已保存' })
        }}
      />
    </div>
  )
}
