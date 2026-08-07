import type { KnowledgeDocDetail } from '../api/types'
import { externalLinkProps } from '../openExternal'

type Props = {
  doc: KnowledgeDocDetail
  busy: boolean
  onClose: () => void
  onRefresh: () => void
  onDelete: () => void
}

function stamp(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function KnowledgeDetail({ doc, busy, onClose, onRefresh, onDelete }: Props) {
  const chunks = doc.chunks || []
  // 同一小节可能切成好几块，目录里只留一次
  const sections = Array.from(
    new Set(chunks.map((c) => c.heading_path).filter(Boolean)),
  )

  return (
    <section className="detail">
      <header className="detail-head">
        <h2 className="detail-title">{doc.title || doc.url}</h2>
        <div className="detail-actions">
          <button type="button" disabled={busy} onClick={onRefresh}>
            {busy ? '拉取中…' : '重新拉取'}
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
        <span className="detail-chip">{doc.scene || 'general'}</span>
        {doc.origin?.startsWith('memory:') ? (
          <span className="detail-chip kind" title="跟着某条记忆里的链接自动抓进来的">
            自动入库
          </span>
        ) : null}
        {(doc.tags || []).map((t) => (
          <span key={t} className="detail-chip tag">
            #{t}
          </span>
        ))}
        <span className="detail-dim">
          {doc.char_count ?? 0} 字 · {doc.chunk_count ?? 0} 块
        </span>
        {doc.fetched_at ? (
          <span className="detail-dim">拉取于 {stamp(doc.fetched_at)}</span>
        ) : null}
      </div>
      <dl className="detail-facts">
        <div className="detail-fact">
          <dt>原文</dt>
          <dd>
            <a {...externalLinkProps(doc.url)}>{doc.url}</a>
          </dd>
        </div>
        {sections.length > 1 ? (
          <div className="detail-fact">
            <dt>小节</dt>
            <dd>{sections.join(' · ')}</dd>
          </div>
        ) : null}
      </dl>
      {doc.last_error ? (
        <div className="detail-body">
          <pre>上次拉取失败：{doc.last_error}</pre>
        </div>
      ) : (
        <div className="detail-body">
          {chunks.map((c) => (
            <div key={c.id} className="kb-chunk">
              {c.heading_path ? <div className="kb-chunk-head">{c.heading_path}</div> : null}
              {/* 正文里常有命令与路径，按原样保留换行 */}
              <pre>{c.text}</pre>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
