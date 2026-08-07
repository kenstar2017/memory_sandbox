type Props = {
  line: string
  apiOk: boolean | null
  apiHint?: string
}

export function StatusBar({ line, apiOk, apiHint }: Props) {
  return (
    <footer className="status-bar">
      <span className={`dot ${apiOk === true ? 'ok' : apiOk === false ? 'bad' : ''}`} />
      <span className="status-text">
        {apiOk === false
          ? apiHint || '无法连接内置 API（请确认已安装 Python 依赖）'
          : line || '就绪'}
      </span>
    </footer>
  )
}
