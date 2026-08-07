import { useEffect, useRef } from 'react'
import { getLongTermRevision } from '../api/client'

const POLL_MS = 5000

/** 同一个接口同时给出记忆与知识库的变更标记，调用方挑一个盯 */
type RevisionField = 'revision' | 'knowledge_revision'

/**
 * 盯着记忆 / 知识库文件的变更标记，别的进程（MCP / CLI / 后台抓取）写入后通知一次。
 *
 * 只 stat 不取内容，所以可以轮询；窗口不可见时停一停，别让后台白跑。
 * 第一次拿到的标记只作为基线，不当成「有新写入」。
 */
export function useMemoryWatch(
  onChange: () => void,
  enabled = true,
  field: RevisionField = 'revision',
) {
  const seen = useRef<string | null>(null)
  const cb = useRef(onChange)
  cb.current = onChange

  useEffect(() => {
    if (!enabled) return
    let stopped = false
    let timer: number | undefined

    const tick = async () => {
      if (stopped) return
      if (!document.hidden) {
        try {
          const revision = (await getLongTermRevision())[field]
          if (!stopped && revision) {
            if (seen.current !== null && revision !== seen.current) {
              cb.current()
            }
            seen.current = revision
          }
        } catch {
          // 后端还没起来 / 短暂失败：下一轮再试，不打扰用户
        }
      }
      if (!stopped) timer = window.setTimeout(tick, POLL_MS)
    }

    void tick()
    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
    }
  }, [enabled, field])
}
