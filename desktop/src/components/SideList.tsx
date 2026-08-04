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
  onDeletePending: (index: number) => void | Promise<void>
  onDeleteSaved: (rec: MemoryRecord) => void | Promise<void>
}

const MAX_VISIBLE_TAGS = 3

function allTags(saved: MemoryRecord[]): string[] {
  const set = new Set<string>()
  saved.forEach((r) => (r.tags || []).forEach((t) => set.add(t)))
  return Array.from(set).sort()
}

/** 正文超过两行左右就折叠，提示去 modal 看全文。 */
function isLong(text: string): boolean {
  return text.length > 56 || text.includes('\n')
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
                  <div className="qa-badges">
                    <span className="badge pending">待补全答</span>
                  </div>
                  <button
                    type="button"
                    className="qa-del"
                    title="删除这条待补全"
                    onClick={(e) => {
                      e.stopPropagation()
                      void onDeletePending(idx)
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
            filtered.map((rec) => {
              const recTags = rec.tags || []
              const extraTags = recTags.length - MAX_VISIBLE_TAGS
              return (
                <div
                  key={rec.id}
                  className="qa-item"
                  onClick={() => onOpenSaved(rec)}
                >
                  <div className="qa-top">
                    <div className="qa-badges">
                      <span className="badge">{rec.scene || 'general'}</span>
                      {rec.kind && rec.kind !== 'qa' ? (
                        <span className="badge kind">{rec.kind}</span>
                      ) : null}
                      {recTags.slice(0, MAX_VISIBLE_TAGS).map((t) => (
                        <span key={t} className="badge tag" title={`#${t}`}>
                          #{t}
                        </span>
                      ))}
                      {extraTags > 0 ? (
                        <span className="badge tag" title={recTags.join(', ')}>
                          +{extraTags}
                        </span>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      className="qa-del"
                      title="删除这条记忆"
                      onClick={(e) => {
                        e.stopPropagation()
                        void onDeleteSaved(rec)
                      }}
                    >
                      删除
                    </button>
                  </div>
                  <p className="qa-q">{rec.question}</p>
                  <p className="qa-a">{rec.answer}</p>
                  {isLong(rec.answer || '') ? (
                    <span className="qa-more">点击查看全文</span>
                  ) : null}
                </div>
              )
            })
          ))}
      </div>
    </aside>
  )
}
