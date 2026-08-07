import { useCallback, useEffect, useState } from 'react'
import { getConfigFile, saveConfigFile, type ConfigView } from '../api/client'
import { alertDialog, confirmDialog } from '../dialogs'

type Props = {
  open: boolean
  onClose: () => void
  onSaved?: (message: string) => void
}

export function ConfigModal({ open, onClose, onSaved }: Props) {
  const [view, setView] = useState<ConfigView | null>(null)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const v = await getConfigFile()
      setView(v)
      setText(v.text)
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  if (!open) return null

  const dirty = !!view && text !== view.text

  const close = async () => {
    if (dirty) {
      const ok = await confirmDialog('改了还没保存，直接关掉？', {
        title: '放弃修改？',
        confirmText: '放弃',
        danger: true,
      })
      if (!ok) return
    }
    onClose()
  }

  const save = async () => {
    setSaving(true)
    try {
      const res = await saveConfigFile(text)
      if (!res.ok) {
        await alertDialog(res.error || '保存失败', { title: '没保存' })
        return
      }
      await load()
      onSaved?.(res.message)
      await alertDialog(res.message, { title: '已保存' })
      onClose()
    } catch (e) {
      void alertDialog(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={() => void close()}>
      <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <h3>配置</h3>
        <p className="modal-hint">
          直接编辑生效的那份 config.yaml。密钥类字段显示为 <code>{view?.mask || '****'}</code>
          ，原样留着就保持不变；要换就把整串掩码替换成新值。
          保存前会先解析一遍，格式或字段不对会拒绝写入并说明原因。
        </p>

        {view ? (
          <div className="hooks-meta">
            <div>
              <span>文件</span>
              <code>{view.path}</code>
            </div>
            {view.masked.length ? (
              <div>
                <span>已遮挡</span>
                <code>{view.masked.join('、')}</code>
              </div>
            ) : null}
            {view.error ? (
              <div>
                <span>读取</span>
                <code>{view.error}</code>
              </div>
            ) : null}
            {!view.exists ? (
              <div>
                <span>注意</span>
                <code>这个文件还不存在，保存后会新建</code>
              </div>
            ) : null}
          </div>
        ) : null}

        <textarea
          className="config-editor"
          spellCheck={false}
          value={loading ? '读取中…' : text}
          disabled={loading || saving}
          onChange={(e) => setText(e.target.value)}
        />

        <p className="modal-hint">
          进程启动时才读配置：保存后 BloomBox 要重启，飞书机器人在它自己的弹窗里点「重启」。
        </p>

        <div className="modal-actions">
          <button type="button" onClick={() => void close()} disabled={saving}>
            关闭窗口
          </button>
          <button type="button" onClick={() => void load()} disabled={loading || saving}>
            重新读取
          </button>
          <button
            type="button"
            className="primary"
            disabled={loading || saving || !dirty}
            onClick={() => void save()}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
