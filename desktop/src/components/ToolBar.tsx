type AgentMode = 'ask' | 'plan' | 'agent'

type Props = {
  agentMode: AgentMode
  onAgentMode: (m: AgentMode) => void
  busy?: boolean
  onAction: (id: string) => void
}

const ACTIONS: { id: string; label: string; danger?: boolean }[] = [
  { id: 'working', label: '短时记忆' },
  { id: 'long', label: '长时记忆' },
  { id: 'all', label: '全部记忆' },
  { id: 'status', label: '记忆状态' },
  { id: 'retrieval', label: '检索设置' },
  { id: 'hooks', label: 'AI 门禁' },
  { id: 'extract', label: '提炼候选' },
  { id: 'clear_w', label: '清空工作记忆' },
  { id: 'backup', label: '备份长时' },
  { id: 'export', label: '导出知识包' },
  { id: 'git', label: '检查过时' },
  { id: 'archive', label: '归档陈旧' },
  { id: 'seed', label: '开发种子' },
  { id: 'data', label: '数据目录' },
  { id: 'clear_l', label: '清空长时', danger: true },
]

export function ToolBar({ agentMode, onAgentMode, busy, onAction }: Props) {
  return (
    <div className="tool-bar">
      <label className="agent-label" title="本地 Cursor LLM 回退：Ask 只读 / Plan 规划 / Agent 可写">
        Agent
        <select
          value={agentMode}
          disabled={busy}
          onChange={(e) => onAgentMode(e.target.value as AgentMode)}
        >
          <option value="ask">Ask 只读</option>
          <option value="plan">Plan 规划</option>
          <option value="agent">Agent 可写</option>
        </select>
      </label>
      <div className="tool-actions">
        {ACTIONS.map((a) => (
          <button
            key={a.id}
            type="button"
            className={a.danger ? 'danger' : ''}
            disabled={busy}
            onClick={() => onAction(a.id)}
          >
            {a.label}
          </button>
        ))}
      </div>
    </div>
  )
}
