/**
 * 应用内对话框。
 *
 * macOS 的 Tauri WebView（wry 未注册 WKUIDelegate 的 JS 对话框处理器）会静默吞掉
 * window.confirm / alert，且 confirm 恒返回 false，导致所有 `if (!confirm()) return`
 * 的操作在桌面端直接失效。这里统一走自绘弹窗，浏览器与桌面端行为一致。
 */

export type DialogKind = 'confirm' | 'alert'

export type DialogOptions = {
  title?: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

export type DialogSpec = DialogOptions & {
  kind: DialogKind
  message: string
}

type Handler = (spec: DialogSpec) => Promise<boolean>

let handler: Handler | null = null

export function registerDialogHandler(next: Handler | null): void {
  handler = next
}

/** 返回用户是否确认；宿主未挂载时按“未确认”处理，避免误执行危险操作。 */
export function confirmDialog(
  message: string,
  options: DialogOptions = {},
): Promise<boolean> {
  if (!handler) return Promise.resolve(false)
  return handler({ kind: 'confirm', message, ...options })
}

export function alertDialog(
  message: string,
  options: DialogOptions = {},
): Promise<void> {
  if (!handler) return Promise.resolve()
  return handler({ kind: 'alert', message, ...options }).then(() => undefined)
}
