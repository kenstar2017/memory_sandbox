import { useEffect, useState } from 'react'
import { suggestQuestion } from '../api/client'
import { alertDialog } from '../dialogs'

export type ModalSeed = {
  question: string
  answer: string
  pendingIndex: number
  id?: string
  tags?: string[]
  kind?: string
  updateOnly?: boolean
}

type Props = {
  open: boolean
  seed: ModalSeed | null
  busy?: boolean
  onClose: () => void
  onConfirm: (payload: {
    question: string
    answer: string
    originalQuestion: string
    id?: string
    updateOnly: boolean
    tags: string[]
    kind: string
  }) => void
}

export function AnswerModal({ open, seed, busy, onClose, onConfirm }: Props) {
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [tags, setTags] = useState('')
  const [kind, setKind] = useState('qa')
  const [originalQuestion, setOriginal] = useState('')
  const [recordId, setRecordId] = useState('')
  const [updateOnly, setUpdateOnly] = useState(false)
  const [suggesting, setSuggesting] = useState(false)

  useEffect(() => {
    if (!open || !seed) return
    setQuestion(seed.question)
    setAnswer(seed.answer || '')
    setTags((seed.tags || []).join(', '))
    setKind(seed.kind || 'qa')
    setOriginal(seed.question)
    setRecordId(seed.id || '')
    setUpdateOnly(!!seed.updateOnly || !!seed.id || seed.pendingIndex < 0)
  }, [open, seed])

  if (!open || !seed) return null

  const parseTags = (raw: string) =>
    raw
      .split(/[,，\s]+/)
      .map((s) => s.replace(/^#/, '').trim())
      .filter(Boolean)

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{updateOnly ? '编辑记忆' : '确认记住'}</h3>
        <label>
          问
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            rows={3}
          />
        </label>
        <div className="modal-inline-actions">
          <button
            type="button"
            className="ghost"
            disabled={suggesting || busy}
            title="按飞书标题/语义重写问法"
            onClick={async () => {
              setSuggesting(true)
              try {
                const data = await suggestQuestion(question, answer)
                if (data.error) {
                  void alertDialog(data.error)
                  return
                }
                if (data.question && data.question !== question) {
                  setQuestion(data.question)
                  if (data.tags?.length && !tags.trim()) {
                    setTags(data.tags.join(', '))
                  }
                } else {
                  void alertDialog(data.hint || '当前问法已较合适，无需改动。')
                }
              } catch (e) {
                void alertDialog(String(e))
              } finally {
                setSuggesting(false)
              }
            }}
          >
            {suggesting ? '优化中…' : '优化问法'}
          </button>
        </div>
        <label>
          答
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={8}
          />
        </label>
        <div className="modal-row">
          <label>
            类型
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="qa">qa</option>
              <option value="command">command</option>
              <option value="path">path</option>
              <option value="env">env</option>
              <option value="pitfall">pitfall</option>
              <option value="decision">decision</option>
            </select>
          </label>
          <label className="grow">
            标签
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="feishu, docs"
            />
          </label>
        </div>
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => {
              if (!question.trim() || !answer.trim()) {
                void alertDialog('问题和答案都不能为空')
                return
              }
              onConfirm({
                question: question.trim(),
                answer: answer.trim(),
                originalQuestion: originalQuestion.trim() || question.trim(),
                id: recordId || undefined,
                updateOnly,
                tags: parseTags(tags),
                kind,
              })
            }}
          >
            {busy ? '保存中…' : '确认记住'}
          </button>
        </div>
      </div>
    </div>
  )
}
