import type { MemoryRecord } from '../api/types'

type Props = {
  tab: 'pending' | 'saved'
  pending: string[]
  saved: MemoryRecord[]
  tagFilter: string
  onTagFilter: (tag: string) => void
  onTab: (t: 'pending' | 'saved') => void
  onOpenPending: (q: string, index: number) => void
  onOpenSaved: (rec: MemoryRecord) => void
  onDeletePending: (index: number) => void
  onDeleteSaved: (rec: MemoryRecord) => void
}

function allTags(saved: MemoryRecord[]): string[] {
  const set = new Set<string>()
  saved.forEach((r) => (r.tags || []).forEach((t) => set.add(t)))
  return Array.from(set).sort()
}

export function SideList({
  tab,
  pending,
  saved,
  tagFilter,
  onTagFilter,
  onTab,
  onOpenPending,
  onOpenSaved,
  onDeletePending,
  onDeleteSaved,
}: Props) {
  const tags = allTags(saved)
  const filtered =
    tab === 'saved' && tagFilter
      ? saved.filter((r) => (r.tags || []).includes(tagFilter))
      : saved

  return (
    <aside className="side">
      <div className="side-title">记忆清单</div>
      <div className="side-tabs">
        <button
          type="button"
          className={tab === 'pending' ? 'active' : ''}
          onClick={() => onTab('pending')}
        >
          待补全 ({pending.length})
        </button>
        <button
          type="button"
          className={tab === 'saved' ? 'active' : ''}
          onClick={() => onTab('saved')}
        >
          已记住 ({saved.length})
        </button>
      </div>
      {tab === 'saved' && tags.length > 0 ? (
        <div className="tag-filter">
          <button
            type="button"
            className={`tag-chip ${tagFilter === '' ? 'active' : ''}`}
            onClick={() => onTagFilter('')}
          >
            全部
          </button>
          {tags.map((t) => (
            <button
              key={t}
              type="button"
              className={`tag-chip ${tagFilter === t ? 'active' : ''}`}
              onClick={() => onTagFilter(t)}
            >
              #{t}
            </button>
          ))}
        </div>
      ) : null}
      <div className="qa-list">
        {tab === 'pending' &&
          (pending.length === 0 ? (
            <div className="qa-empty">暂无待补全。点「记忆」添加问题。</div>
          ) : (
            pending.map((q, idx) => (
              <div
                key={`${idx}-${q}`}
                className="qa-item pending"
                onClick={() => onOpenPending(q, idx)}
              >
                <div className="qa-top">
                  <span className="badge pending">待补全答</span>
                  <button
                    type="button"
                    className="qa-del"
                    onClick={(e) => {
                      e.stopPropagation()
                      onDeletePending(idx)
                    }}
                  >
                    删除
                  </button>
                </div>
                <p className="qa-q">{q}</p>
                <p className="qa-a">点击填写答案</p>
              </div>
            ))
          ))}
        {tab === 'saved' &&
          (saved.length === 0 ? (
            <div className="qa-empty">暂无已记住的问答。</div>
          ) : filtered.length === 0 ? (
            <div className="qa-empty">当前标签下无记忆。</div>
          ) : (
            filtered.map((rec) => (
              <div
                key={rec.id}
                className="qa-item"
                onClick={() => onOpenSaved(rec)}
              >
                <div className="qa-top">
                  <span className="badge">{rec.scene || 'general'}</span>
                  {rec.kind && rec.kind !== 'qa' ? (
                    <span className="badge kind">{rec.kind}</span>
                  ) : null}
                  {(rec.tags || []).slice(0, 3).map((t) => (
                    <span key={t} className="badge tag">
                      #{t}
                    </span>
                  ))}
                  <button
                    type="button"
                    className="qa-del"
                    onClick={(e) => {
                      e.stopPropagation()
                      onDeleteSaved(rec)
                    }}
                  >
                    删除
                  </button>
                </div>
                <p className="qa-q">{rec.question}</p>
                <p className="qa-a">{rec.answer}</p>
              </div>
            ))
          ))}
      </div>
    </aside>
  )
}
