import { openUrl } from '@tauri-apps/plugin-opener'

/**
 * 用系统浏览器打开外链。
 *
 * 外链一律走这里，别退回裸 `<a target="_blank">`：WebView 里点它毫无反应——Tauri
 * 既不会为它开新窗口，也不会转交给系统浏览器，看起来就像链接是死的。
 *
 * 仍然保留 `href`（右键复制链接、无障碍读屏都靠它），只是在 onClick 里接管跳转。
 */
export async function openExternal(url: string): Promise<void> {
  const target = (url || '').trim()
  if (!target) return
  try {
    await openUrl(target)
  } catch {
    // 浏览器里跑 vite dev 时没有 Tauri 运行时，这条路才是能用的那条
    window.open(target, '_blank', 'noopener,noreferrer')
  }
}

/** 配 `<a href=…>` 用：拦下默认跳转，改走系统浏览器。 */
export function externalLinkProps(url: string) {
  return {
    href: url,
    onClick: (e: React.MouseEvent) => {
      e.preventDefault()
      void openExternal(url)
    },
  }
}
