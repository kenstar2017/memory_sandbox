import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getFeishuBotStatus,
  restartFeishuBot,
  startFeishuBot,
  stopFeishuBot,
  type FeishuBotStatus,
} from '../api/client'
import { alertDialog, confirmDialog } from '../dialogs'

type Props = {
  open: boolean
  onClose: () => void
  onChanged?: (status: FeishuBotStatus) => void
}

// 机器人在跑的时候日志才有新内容，隔几秒刷一次就够看出「连上没有」
const POLL_MS = 4000

function headline(st: FeishuBotStatus): string {
  if (!st.available) return '安装包里没有 feishu_bot.py，起不来'
  if (!st.sdk_installed) return '缺 lark-oapi：pip install lark-oapi'
  if (!st.configured) return '还没配 app_id / app_secret，配完才能启动'
  if (!st.running) return '未运行：飞书里发消息不会有人应'
  if (!st.owned) return `运行中（PID ${st.pid}，在 BloomBox 之外启动的）`
  return `运行中（PID ${st.pid}）`
}

export function FeishuBotModal({ open, onClose, onChanged }: Props) {
  const [status, setStatus] = useState<FeishuBotStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)

  // silent 给轮询用：轮询不该动 loading，否则「刷新」按钮每 4 秒自己灰一下，
  // 弹窗也会闪一下「读取中…」
  const refresh = useCallback(
    async (silent = false) => {
      if (!silent) setLoading(true)
      try {
        const st = await getFeishuBotStatus()
        setStatus(st)
        onChanged?.(st)
      } catch (e) {
        if (!silent) void alertDialog(String(e))
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [onChanged],
  )

  useEffect(() => {
    if (!open) return
    void refresh()
    const timer = window.setInterval(() => {
      // 启停过程中别插队刷新，免得把「启动中…」的状态覆盖成上一秒的旧值
      if (!busyRef.current) void refresh(true)
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [open, refresh])

  if (!open) return null

  const run = async (
    action: () => Promise<{ ok: boolean; message: string; status: FeishuBotStatus }>,
    title: string,
  ) => {
    setBusy(true)
    busyRef.current = true
    try {
      const res = await action()
      setStatus(res.status)
      onChanged?.(res.status)
      await alertDialog(res.message, { title })
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  const stop = async () => {
    const ok = await confirmDialog('停掉之后飞书里发消息、文档评论都不会有人应了。', {
      title: '停止飞书机器人？',
      confirmText: '停止',
      danger: true,
    })
    if (!ok) return
    await run(stopFeishuBot, '已停止')
  }

  const canStart = !!status && status.available && status.sdk_installed && status.configured

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>飞书机器人</h3>
        <p className="modal-hint">
          常驻的长连接进程：飞书里私聊或群里 @ 它就能查、写这同一份记忆库。
          它自成一个会话，<b>关掉 BloomBox 也会继续跑</b>，想让它下线就在这里停。
        </p>

        {loading && !status ? (
          <p className="modal-hint">读取中…</p>
        ) : status ? (
          <>
            <p className={status.running ? 'hooks-ok' : 'hooks-warn'}>{headline(status)}</p>
            <div className="hooks-meta">
              {status.started_at ? (
                <div>
                  <span>启动时间</span>
                  <code>{status.started_at}</code>
                </div>
              ) : null}
              <div>
                <span>白名单</span>
                <code>
                  {status.allow_count
                    ? `${status.allow_count} 人`
                    : '空（谁都不服务，只回对方自己的 open_id）'}
                </code>
              </div>
              <div>
                <span>文档评论机器人</span>
                <code>{status.doc_bot_enabled ? '开' : '关（feishu.doc_bot_enabled）'}</code>
              </div>
              <div>
                <span>日志</span>
                <code>{status.log}</code>
              </div>
              {status.error ? (
                <div>
                  <span>读配置</span>
                  <code>{status.error}</code>
                </div>
              ) : null}
            </div>
            <pre className="bot-log">{status.log_tail || '（还没有日志）'}</pre>
          </>
        ) : null}

        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            关闭窗口
          </button>
          <button type="button" onClick={() => void refresh()} disabled={busy || loading}>
            刷新
          </button>
          {status?.running ? (
            <>
              <button type="button" className="danger" disabled={busy} onClick={stop}>
                {busy ? '处理中…' : '停止'}
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() => void run(restartFeishuBot, '已重启')}
              >
                {busy ? '重启中…' : '重启'}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={busy || !canStart}
              onClick={() => void run(startFeishuBot, '已启动')}
            >
              {busy ? '启动中…' : '启动'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
