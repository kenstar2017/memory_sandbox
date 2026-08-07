import type { MemoryRecord } from '../api/types'

type Props = {
  rec: MemoryRecord
  onClose: () => void
  onEdit: () => void
  onDelete: () => void
}

function stamp(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function MemoryDetail({ rec, onClose, onEdit, onDelete }: Props) {
  const facts = Object.entries(rec.facts || {})
  const tags = rec.tags || []

  return (
    <section className="detail">
      <header className="detail-head">
        <h2 className="detail-title">{rec.question}</h2>
        <div className="detail-actions">
          <button type="button" onClick={onEdit}>
            编辑
          </button>
          <button type="button" className="detail-danger" onClick={onDelete}>
            删除
          </button>
          <button type="button" onClick={onClose}>
            返回对话
          </button>
        </div>
      </header>
      <div className="detail-meta">
        <span className="detail-chip">{rec.scene || 'general'}</span>
        {rec.kind && rec.kind !== 'qa' ? (
          <span className="detail-chip kind">{rec.kind}</span>
        ) : null}
        {tags.map((t) => (
          <span key={t} className="detail-chip tag">
            #{t}
          </span>
        ))}
        <span className="detail-dim">
          命中 {rec.hit_count ?? 0} · 权重 {rec.weight ?? 1}
        </span>
        {rec.created_at ? (
          <span className="detail-dim">记于 {stamp(rec.created_at)}</span>
        ) : null}
      </div>
      {facts.length > 0 ? (
        <dl className="detail-facts">
          {facts.map(([k, v]) => (
            <div key={k} className="detail-fact">
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <div className="detail-body">
        {/* 正文常含命令与路径，按原样保留换行与缩进 */}
        <pre>{rec.answer}</pre>
      </div>
    </section>
  )
}
