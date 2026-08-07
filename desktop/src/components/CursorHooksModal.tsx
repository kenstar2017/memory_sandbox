import { useCallback, useEffect, useState } from 'react'
import {
  getCursorHooksStatus,
  installCursorHooks,
  uninstallCursorHooks,
  type CursorHooksStatus,
} from '../api/client'
import { alertDialog, confirmDialog } from '../dialogs'

type Props = {
  open: boolean
  onClose: () => void
  onChanged?: (status: CursorHooksStatus) => void
}

function headline(st: CursorHooksStatus): string {
  if (st.error) return `读取配置失败：${st.error}`
  if (!st.available) return '当前安装包里缺少 hook 脚本，无法安装'
  if (!st.installed) return '未启用：AI 可以不查记忆就动手，也不会被追问落库'
  if (!st.up_to_date) return '已启用，但脚本是旧版，建议重新安装以更新'
  return '已启用：AI 动手前会被要求先查记忆，结束没落库会被追问一轮'
}

export function CursorHooksModal({ open, onClose, onChanged }: Props) {
  const [status, setStatus] = useState<CursorHooksStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const st = await getCursorHooksStatus()
      setStatus(st)
      onChanged?.(st)
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setLoading(false)
    }
  }, [onChanged])

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  if (!open) return null

  const enable = async () => {
    setBusy(true)
    try {
      const res = await installCursorHooks()
      await refresh()
      await alertDialog(
        `${res.message}\n\n已挂载：${res.events.join('、')}\n` +
          (res.backup ? `原配置已备份到 ${res.backup}` : '（原先没有 hooks.json，已新建）'),
        { title: '已启用记忆门禁' },
      )
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setBusy(false)
    }
  }

  const disable = async () => {
    const ok = await confirmDialog(
      '关闭后 AI 不再被强制先查记忆、也不会被追问落库。你自己配的其它 hook 会原样保留。',
      { title: '关闭记忆门禁？', confirmText: '关闭', danger: true },
    )
    if (!ok) return
    setBusy(true)
    try {
      const res = await uninstallCursorHooks()
      await refresh()
      await alertDialog(res.message, { title: '已关闭' })
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>AI 记忆门禁</h3>
        <p className="modal-hint">
          给 Cursor 装一组 hook，让所有项目里的 AI 都「动手前先查记忆、结束前把结论落库」。
          只拦改文件、删文件、拉子 agent 和飞书写操作；查看代码、搜索、执行命令不受影响。
        </p>

        {loading && !status ? (
          <p className="modal-hint">读取中…</p>
        ) : status ? (
          <>
            <p className={status.installed && status.up_to_date ? 'hooks-ok' : 'hooks-warn'}>
              {headline(status)}
            </p>
            <div className="hooks-meta">
              <div>
                <span>配置文件</span>
                <code>{status.hooks_json}</code>
              </div>
              <div>
                <span>脚本目录</span>
                <code>{status.hooks_dir}</code>
              </div>
              <div>
                <span>解释器</span>
                <code>{status.python}</code>
              </div>
              {status.installed_at ? (
                <div>
                  <span>安装时间</span>
                  <code>{status.installed_at}</code>
                </div>
              ) : null}
              <div>
                <span>你自己的 hook</span>
                <code>{status.foreign_entries} 条（安装与关闭都不会动）</code>
              </div>
              {status.stale_scripts.length ? (
                <div>
                  <span>待更新</span>
                  <code>{status.stale_scripts.join('、')}</code>
                </div>
              ) : null}
            </div>
            <p className="modal-hint">
              注入协议要新开一个对话才生效；改文件的门禁存盘即生效。
            </p>
          </>
        ) : null}

        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            关闭窗口
          </button>
          {status?.installed ? (
            <>
              <button type="button" className="danger" disabled={busy} onClick={disable}>
                关闭门禁
              </button>
              {!status.up_to_date ? (
                <button type="button" className="primary" disabled={busy} onClick={enable}>
                  {busy ? '更新中…' : '更新脚本'}
                </button>
              ) : null}
            </>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={busy || !status?.available}
              onClick={enable}
            >
              {busy ? '启用中…' : '启用门禁'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
