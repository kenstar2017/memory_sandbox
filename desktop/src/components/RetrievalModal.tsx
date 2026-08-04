import { useEffect, useState } from 'react'
import { getRetrievalSettings, setRetrievalSettings } from '../api/client'
import { alertDialog } from '../dialogs'

type Props = {
  open: boolean
  onClose: () => void
  onSaved: (statusLine?: string) => void
}

export function RetrievalModal({ open, onClose, onSaved }: Props) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({
    similarity_threshold: 0.72,
    top_k: 5,
    bm25_enabled: true,
    vector_weight: 0.55,
    keyword_weight: 0.25,
    bm25_weight: 0.2,
    aging_enabled: true,
    aging_days: 30,
    aging_decay: 0.15,
  })

  useEffect(() => {
    if (!open) return
    setLoading(true)
    getRetrievalSettings()
      .then((data) => {
        const s = (data.values || data.settings || data) as Record<string, unknown>
        setForm((prev) => ({
          ...prev,
          similarity_threshold: Number(s.similarity_threshold ?? prev.similarity_threshold),
          top_k: Number(s.top_k ?? prev.top_k),
          bm25_enabled: Boolean(s.bm25_enabled ?? prev.bm25_enabled),
          vector_weight: Number(s.vector_weight ?? prev.vector_weight),
          keyword_weight: Number(s.keyword_weight ?? prev.keyword_weight),
          bm25_weight: Number(s.bm25_weight ?? prev.bm25_weight),
          aging_enabled: Boolean(s.aging_enabled ?? prev.aging_enabled),
          aging_days: Number(s.aging_days ?? prev.aging_days),
          aging_decay: Number(s.aging_decay ?? prev.aging_decay),
        }))
      })
      .catch((e) => void alertDialog(String(e)))
      .finally(() => setLoading(false))
  }, [open])

  if (!open) return null

  const num = (key: keyof typeof form, label: string, step = 0.01) => (
    <label key={key}>
      {label}
      <input
        type="number"
        step={step}
        value={form[key] as number}
        onChange={(e) =>
          setForm((f) => ({ ...f, [key]: Number(e.target.value) }))
        }
      />
    </label>
  )

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>检索设置</h3>
        <p className="modal-hint">调整长时记忆打分与命中阈值，保存后写入本机配置。</p>
        {loading ? (
          <p className="modal-hint">加载中…</p>
        ) : (
          <div className="retrieval-grid">
            {num('similarity_threshold', '命中阈值')}
            {num('top_k', 'Top K', 1)}
            {num('vector_weight', '向量权重')}
            {num('keyword_weight', '关键词权重')}
            {num('bm25_weight', 'BM25 权重')}
            <label className="check">
              <input
                type="checkbox"
                checked={form.bm25_enabled}
                onChange={(e) =>
                  setForm((f) => ({ ...f, bm25_enabled: e.target.checked }))
                }
              />
              启用 BM25
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={form.aging_enabled}
                onChange={(e) =>
                  setForm((f) => ({ ...f, aging_enabled: e.target.checked }))
                }
              />
              陈旧降权
            </label>
            {num('aging_days', '陈旧天数', 1)}
            {num('aging_decay', '陈旧衰减')}
          </div>
        )}
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button
            type="button"
            className="primary"
            disabled={saving || loading}
            onClick={async () => {
              setSaving(true)
              try {
                const data = await setRetrievalSettings(form)
                onSaved(typeof data.status_line === 'string' ? data.status_line : undefined)
                onClose()
              } catch (e) {
                void alertDialog(String(e))
              } finally {
                setSaving(false)
              }
            }}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
