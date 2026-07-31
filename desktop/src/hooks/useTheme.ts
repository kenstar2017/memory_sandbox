import { useEffect, useState } from 'react'
import {
  THEME_KEY,
  applyTheme,
  loadThemePreference,
  type ThemePreference,
} from '../theme'

export function useTheme() {
  const [preference, setPreferenceState] = useState<ThemePreference>(() =>
    loadThemePreference(),
  )

  useEffect(() => {
    applyTheme(preference)
    try {
      localStorage.setItem(THEME_KEY, preference)
    } catch {
      /* ignore */
    }
  }, [preference])

  useEffect(() => {
    if (preference !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => applyTheme('system')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [preference])

  const setPreference = (next: ThemePreference) => {
    setPreferenceState(next)
  }

  return { preference, setPreference }
}
