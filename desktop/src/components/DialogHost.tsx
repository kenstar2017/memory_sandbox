import { useEffect, useState } from 'react'
import { registerDialogHandler, type DialogSpec } from '../dialogs'

type Pending = DialogSpec & { resolve: (ok: boolean) => void }

export function DialogHost() {
  const [pending, setPending] = useState<Pending | null>(null)

  useEffect(() => {
    registerDialogHandler(
      (spec) => new Promise<boolean>((resolve) => setPending({ ...spec, resolve })),
    )
    return () => registerDialogHandler(null)
  }, [])

  useEffect(() => {
    if (!pending) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        pending.resolve(false)
        setPending(null)
      } else if (e.key === 'Enter') {
        e.preventDefault()
        pending.resolve(true)
        setPending(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pending])

  if (!pending) return null

  const isAlert = pending.kind === 'alert'
  const done = (ok: boolean) => {
    pending.resolve(ok)
    setPending(null)
  }

  return (
    <div className="modal-backdrop" onClick={() => done(false)}>
      <div className="modal confirm-modal" onClick={(e) => e.stopPropagation()}>
        <h3>{pending.title || (isAlert ? '提示' : '请确认')}</h3>
        <p className="confirm-message">{pending.message}</p>
        <div className="modal-actions">
          {isAlert ? null : (
            <button type="button" onClick={() => done(false)}>
              {pending.cancelText || '取消'}
            </button>
          )}
          <button
            type="button"
            className={pending.danger ? 'primary danger' : 'primary'}
            autoFocus
            onClick={() => done(true)}
          >
            {pending.confirmText || (isAlert ? '知道了' : '确定')}
          </button>
        </div>
      </div>
    </div>
  )
}
