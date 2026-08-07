import { useState } from 'react'
import type { KnowledgeDoc, MemoryRecord } from '../api/types'

export type SideTab = 'pending' | 'saved' | 'knowledge'

type Props = {
  tab: SideTab
  pending: string[]
  saved: MemoryRecord[]
  docs: KnowledgeDoc[]
  tagFilter: string
  query: string
  activeId?: string
  /** 别处（MCP / CLI）新写入、还没看过的记忆 */
  newIds: Set<string>
  /** 别处原地覆盖了内容、还没看过的记忆（条数不变，容易被忽略） */
  changedIds: Set<string>
  /** 正在抓取的那篇（同步接口，长文档要等几秒） */
  addingDoc: boolean
  onSeenAll: () => void
  onQuery: (q: string) => void
  onTagFilter: (tag: string) => void
  onTab: (t: SideTab) => void
  onOpenPending: (q: string, index: number) => void
  onOpenSaved: (rec: MemoryRecord) => void
  onDeletePending: (index: number) => void | Promise<void>
  onDeleteSaved: (rec: MemoryRecord) => void | Promise<void>
  onAddDoc: (url: string) => void | Promise<void>
  onOpenDoc: (doc: KnowledgeDoc) => void
  onDeleteDoc: (doc: KnowledgeDoc) => void | Promise<void>
  onBackfillDocs: () => void | Promise<void>
}

function docHint(doc: KnowledgeDoc): string {
  if (doc.last_error) return `${doc.title}\n\n上次抓取失败：${doc.last_error}`
  const size = doc.char_count ? `${doc.char_count} 字` : ''
  const chunks = doc.chunk_count ? `${doc.chunk_count} 块` : ''
  const from = doc.origin?.startsWith('memory:') ? '（跟着记忆里的链接自动入库）' : ''
  return `${doc.title}\n${[size, chunks].filter(Boolean).join(' · ')}${from}\n${doc.url}`
}

function tagCounts(saved: MemoryRecord[]): [string, number][] {
  const counts = new Map<string, number>()
  saved.forEach((r) =>
    (r.tags || []).forEach((t) => counts.set(t, (counts.get(t) || 0) + 1)),
  )
  return Array.from(counts.entries()).sort((a, b) => a[0].localeCompare(b[0]))
}

/** 标题一行放不下时靠悬停认条目，所以提示里带上正文开头。 */
function rowHint(rec: MemoryRecord, edited = false): string {
  const mark = edited ? '（内容刚被别处改写）\n' : ''
  const body = (rec.answer || '').replace(/\s+/g, ' ')
  if (!body) return mark + rec.question
  const head = body.slice(0, 160)
  return `${mark}${rec.question}\n\n${head}${body.length > 160 ? '…' : ''}`
}

function matches(rec: MemoryRecord, needle: string): boolean {
  if (!needle) return true
  return (
    (rec.question || '').toLowerCase().includes(needle) ||
    (rec.answer || '').toLowerCase().includes(needle) ||
    (rec.tags || []).some((t) => t.toLowerCase().includes(needle))
  )
}

export function SideList({
  tab,
  pending,
  saved,
  docs,
  tagFilter,
  query,
  activeId,
  newIds,
  changedIds,
  addingDoc,
  onSeenAll,
  onQuery,
  onTagFilter,
  onTab,
  onOpenPending,
  onOpenSaved,
  onDeletePending,
  onDeleteSaved,
  onAddDoc,
  onOpenDoc,
  onDeleteDoc,
  onBackfillDocs,
}: Props) {
  const [docUrl, setDocUrl] = useState('')
  const needle = query.trim().toLowerCase()
  const tags = tagCounts(saved)
  const visibleDocs = docs.filter(
    (d) =>
      !needle ||
      (d.title || '').toLowerCase().includes(needle) ||
      (d.url || '').toLowerCase().includes(needle),
  )

  const submitDoc = () => {
    const url = docUrl.trim()
    if (!url || addingDoc) return
    void Promise.resolve(onAddDoc(url)).then(() => setDocUrl(''))
  }
  const filtered = saved.filter(
    (r) =>
      (!tagFilter || (r.tags || []).includes(tagFilter)) && matches(r, needle),
  )
  // 先带上原始下标再过滤：删除按的是 pending 里的位置，不能用 indexOf 反查
  const visiblePending = pending
    .map((q, index) => ({ q, index }))
    .filter(({ q }) => !needle || q.toLowerCase().includes(needle))

  return (
    <aside className="side">
      <div className="side-title">
        记忆清单
        {newIds.size > 0 ? (
          <button
            type="button"
            className="side-new"
            title="别处刚写入的新记忆，点这里标为已看"
            onClick={onSeenAll}
          >
            {newIds.size} 条新
          </button>
        ) : null}
        {changedIds.size > 0 ? (
          <button
            type="button"
            className="side-new edited"
            title="别处原地改写了这些记忆的内容，点这里标为已看"
            onClick={onSeenAll}
          >
            {changedIds.size} 条改
          </button>
        ) : null}
      </div>
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
        <button
          type="button"
          className={tab === 'knowledge' ? 'active' : ''}
          onClick={() => onTab('knowledge')}
        >
          知识库 ({docs.length})
        </button>
      </div>
      <div className="side-filters">
        <input
          className="side-search"
          type="search"
          value={query}
          placeholder={tab === 'knowledge' ? '搜索文档标题 / 链接' : '搜索标题 / 正文 / 标签'}
          onChange={(e) => onQuery(e.target.value)}
        />
        {tab === 'knowledge' ? (
          <div className="side-add-doc">
            <input
              type="text"
              value={docUrl}
              disabled={addingDoc}
              placeholder="粘贴飞书文档链接，回车录入"
              onChange={(e) => setDocUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitDoc()
              }}
            />
            <button type="button" disabled={addingDoc || !docUrl.trim()} onClick={submitDoc}>
              {addingDoc ? '抓取中…' : '录入'}
            </button>
          </div>
        ) : null}
        {tab === 'knowledge' ? (
          <button
            type="button"
            className="side-backfill"
            disabled={addingDoc}
            title="扫一遍已记住的记忆，把里面出现过、还没入库的飞书文档补录进来（后台抓取）"
            onClick={() => void onBackfillDocs()}
          >
            从记忆补录
          </button>
        ) : null}
        {tab === 'saved' && tags.length > 0 ? (
          // 标签做成下拉：上百个标签铺成 chip 会把整个列表顶出可视区
          <select
            className="side-tag-select"
            value={tagFilter}
            onChange={(e) => onTagFilter(e.target.value)}
          >
            <option value="">全部标签（{saved.length}）</option>
            {tags.map(([t, n]) => (
              <option key={t} value={t}>
                #{t}（{n}）
              </option>
            ))}
          </select>
        ) : null}
      </div>
      <div className="qa-list">
        {tab === 'pending' &&
          (visiblePending.length === 0 ? (
            <div className="qa-empty">
              {pending.length === 0
                ? '暂无待补全。点「记忆」添加问题。'
                : '没有匹配的待补全。'}
            </div>
          ) : (
            visiblePending.map(({ q, index }) => (
              <div
                key={`${index}-${q}`}
                className="qa-row pending"
                title={q}
                onClick={() => onOpenPending(q, index)}
              >
                <span className="qa-row-dot" aria-hidden="true" />
                <span className="qa-row-title">{q}</span>
                <button
                  type="button"
                  className="qa-row-del"
                  title="删除这条待补全"
                  onClick={(e) => {
                    e.stopPropagation()
                    void onDeletePending(index)
                  }}
                >
                  ×
                </button>
              </div>
            ))
          ))}
        {tab === 'saved' &&
          (saved.length === 0 ? (
            <div className="qa-empty">暂无已记住的问答。</div>
          ) : filtered.length === 0 ? (
            <div className="qa-empty">没有匹配的记忆。</div>
          ) : (
            filtered.map((rec) => (
              <div
                key={rec.id}
                className={`qa-row ${activeId === rec.id ? 'active' : ''} ${
                  newIds.has(rec.id) || changedIds.has(rec.id) ? 'fresh' : ''
                }`}
                title={rowHint(rec, changedIds.has(rec.id))}
                onClick={() => onOpenSaved(rec)}
              >
                {newIds.has(rec.id) ? (
                  <span className="qa-row-dot fresh" aria-label="新写入" />
                ) : changedIds.has(rec.id) ? (
                  <span className="qa-row-dot edited" aria-label="内容被改写" />
                ) : null}
                <span className="qa-row-title">{rec.question}</span>
                <button
                  type="button"
                  className="qa-row-del"
                  title="删除这条记忆"
                  onClick={(e) => {
                    e.stopPropagation()
                    void onDeleteSaved(rec)
                  }}
                >
                  ×
                </button>
              </div>
            ))
          ))}
        {tab === 'knowledge' &&
          (docs.length === 0 ? (
            <div className="qa-empty">
              知识库还是空的。粘贴飞书文档链接录入，
              或者在记忆里带上链接——写入时会自动抓进来。
            </div>
          ) : visibleDocs.length === 0 ? (
            <div className="qa-empty">没有匹配的文档。</div>
          ) : (
            visibleDocs.map((doc) => (
              <div
                key={doc.id}
                className={`qa-row ${activeId === doc.id ? 'active' : ''} ${
                  doc.last_error ? 'failed' : ''
                }`}
                title={docHint(doc)}
                onClick={() => onOpenDoc(doc)}
              >
                {doc.last_error ? (
                  <span className="qa-row-dot failed" aria-label="抓取失败" />
                ) : null}
                <span className="qa-row-title">{doc.title || doc.url}</span>
                <button
                  type="button"
                  className="qa-row-del"
                  title="从知识库移除这篇"
                  onClick={(e) => {
                    e.stopPropagation()
                    void onDeleteDoc(doc)
                  }}
                >
                  ×
                </button>
              </div>
            ))
          ))}
      </div>
      {tab === 'saved' && (tagFilter || needle) ? (
        <div className="side-foot">
          显示 {filtered.length} / {saved.length}
          <button type="button" onClick={() => { onTagFilter(''); onQuery('') }}>
            清除筛选
          </button>
        </div>
      ) : null}
    </aside>
  )
}
