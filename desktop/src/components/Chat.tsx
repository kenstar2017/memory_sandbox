import { useEffect, useRef, type ReactNode } from 'react'
import type { ChatMessage } from '../api/types'
import { THEME_OPTIONS, type ThemePreference } from '../theme'
import { ToolBar } from './ToolBar'

const SOURCE: Record<string, string> = {
  working: '工作记忆',
  long_term: '长时记忆',
  procedural: '程序性记忆',
  llm: '大模型',
  command: '指令',
  sensory_reject: '感觉记忆',
  miss: '未命中',
}

type AgentMode = 'ask' | 'plan' | 'agent'
type InputMode = 'chat' | 'memory' | 'extract'

type Props = {
  messages: ChatMessage[]
  busy: boolean
  inputMode: InputMode
  input: string
  onInput: (v: string) => void
  onSend: () => void
  onSetMode: (m: InputMode) => void
  theme: ThemePreference
  onThemeChange: (t: ThemePreference) => void
  agentMode: AgentMode
  onAgentMode: (m: AgentMode) => void
  onToolAction: (id: string) => void
  /** null = 还没问到（API 没起来或查询失败） */
  botRunning: boolean | null
  onBot: () => void
  onConfig: () => void
  /**
   * 给了就顶掉消息区与输入框（点左侧标题看记忆详情用）。
   * 顶栏与工具栏照旧显示，否则主题、「记忆」、「AI 门禁」这些入口会一起消失。
   */
  detail?: ReactNode
}

export function Chat({
  messages,
  busy,
  inputMode,
  input,
  onInput,
  onSend,
  onSetMode,
  theme,
  onThemeChange,
  agentMode,
  onAgentMode,
  onToolAction,
  botRunning,
  onBot,
  onConfig,
  detail,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const modeHint =
    inputMode === 'memory'
      ? '当前指令：记忆 — 请输入「问」，Enter 加入左侧待补全'
      : inputMode === 'extract'
        ? '当前指令：提炼 — 粘贴终端/日志，Enter 提炼候选'
        : null

  // 顶栏与工具栏在对话和详情两种视图里都要在
  const chrome = (
    <>
      <header className="toolbar">
        <h1>BloomBox</h1>
        <div className="toolbar-actions">
          <label className="theme-label" title="外观主题">
            <select
              className="theme-select"
              value={theme}
              aria-label="主题"
              onChange={(e) => onThemeChange(e.target.value as ThemePreference)}
            >
              {THEME_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className={inputMode === 'memory' ? 'active-btn' : ''}
            onClick={() => onSetMode(inputMode === 'memory' ? 'chat' : 'memory')}
          >
            {inputMode === 'memory' ? '记忆中…' : '记忆'}
          </button>
          <button
            type="button"
            className="bot-btn"
            title={
              botRunning === true
                ? '飞书机器人运行中，点击查看日志或停止'
                : botRunning === false
                  ? '飞书机器人未运行，点击启动'
                  : '飞书机器人状态未知'
            }
            onClick={onBot}
          >
            {/* 顶栏放它就是为了一眼看出在不在跑；停着是常态不是故障，所以不用红点 */}
            <span className={`dot ${botRunning ? 'ok' : ''}`} />
            飞书机器人
          </button>
          <button type="button" title="查看 / 修改生效的 config.yaml" onClick={onConfig}>
            配置
          </button>
        </div>
      </header>
      <ToolBar
        agentMode={agentMode}
        onAgentMode={onAgentMode}
        busy={busy}
        onAction={onToolAction}
      />
    </>
  )

  if (detail) {
    return (
      <section className="main">
        {chrome}
        {detail}
      </section>
    )
  }

  return (
    <section className="main">
      {chrome}
      {modeHint ? (
        <div className="mode-bar">
          <span>{modeHint}</span>
          <button type="button" onClick={() => onSetMode('chat')}>
            取消
          </button>
        </div>
      ) : null}
      <div className="chat">
        {messages.map((m) => {
          if (m.role === 'think') {
            return (
              <div
                key={m.id}
                className={`think-card ${m.done ? 'done' : ''} ${m.error ? 'error' : ''}`}
              >
                <div className="think-head">
                  <span className="think-title">
                    {m.error ? '思考中断' : m.done ? '思考完成' : '思考中'}
                  </span>
                </div>
                <ul className="think-steps">
                  {m.steps.map((s, i) => (
                    <li
                      key={i}
                      className={
                        i === m.steps.length - 1 && !m.done && !m.error
                          ? 'active'
                          : 'done'
                      }
                    >
                      <span className="mark">
                        {i === m.steps.length - 1 && !m.done && !m.error
                          ? '●'
                          : m.error && i === m.steps.length - 1
                            ? '!'
                            : '✓'}
                      </span>
                      <span>{s}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )
          }
          return (
            <div key={m.id} className={`msg ${m.role}`}>
              {m.role === 'bot' && m.label ? (
                <div className="msg-label">{m.label}</div>
              ) : null}
              <pre className="msg-body">{m.text}</pre>
            </div>
          )
        })}
        <div ref={bottomRef} />
      </div>
      <div className="composer">
        <textarea
          value={input}
          disabled={busy}
          placeholder={
            inputMode === 'memory'
              ? '记忆模式：输入问题（问），Enter 加入左侧'
              : inputMode === 'extract'
                ? '提炼模式：粘贴终端输出或日志，Enter 提炼'
                : '问就直接问；要记东西说「记一下 …」即可'
          }
          rows={2}
          onChange={(e) => onInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              onSend()
            }
          }}
        />
        <button type="button" className="primary" disabled={busy || !input.trim()} onClick={onSend}>
          发送
        </button>
      </div>
    </section>
  )
}

export function sourceLabel(source: string): string {
  return SOURCE[source] || source
}
