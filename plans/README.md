# 动效改进方案

由 `improve-animations` skill（`.agents/skills/improve-animations/`）对 `desktop/src/`
做了一次动效体检后产出。每份方案都是自包含的：执行者不需要读这份 README、不需要看体检过程，
照着单个方案文件就能改。方案只描述改动，**不包含已完成的改动**——状态见下表。

审计基线 commit：`71cbfdd`。若当前代码已明显漂移，先跑一次
`improve-animations reconcile` 刷新方案里的 file:line，再执行。

## 方案表

| # | 标题 | 严重度 | 类别 | 状态 |
| --- | --- | --- | --- | --- |
| [001](001-motion-tokens.md) | 建立 easing / duration token 地基 | MEDIUM | 一致性 | TODO |
| [002](002-press-feedback.md) | 给按钮加按压反馈，并补齐缺失的 hover | MEDIUM | 物理性 + 可访问性 | TODO |
| [003](003-chat-scroll.md) | 流式回答期间停掉 smooth 滚动 | HIGH | 频率 + 性能 | TODO |
| [004](004-overlay-enter.md) | 给六个浮层加进场动效（只做进场） | MEDIUM | 可打断性 | TODO |

## 推荐执行顺序与依赖

```
001 ──> 002 ──> 004
             （002 建立 prefers-reduced-motion 块，004 复用）

003 独立，可以随时插队执行
```

1. **001 必须最先**：它只加 token、不产生任何视觉变化，002 和 004 都要消费
   `--ease-out` / `--dur-*` / `--press-scale` / `--enter-scale`。跳过它就会出现手打的
   `cubic-bezier`，正是体检要消除的问题。
2. **002 在 001 之后**：它同时建立 `@media (prefers-reduced-motion: reduce)` 块，
   在里面把 `--press-scale` 和 `--enter-scale` 覆盖为 `1`。
3. **004 在 002 之后**：它依赖那个 reduced-motion 块已经存在。如果先做 004，
   进场的放大就没有减弱动效的兜底。
4. **003 与其它三份无依赖**：唯一改 `.tsx` 的方案，也是四份里唯一 HIGH。
   想尽早见效就先做它。

003 改 `desktop/src/components/Chat.tsx`；001、002、004 都只改 `desktop/src/App.css`，
所以 003 可以和其它任意一份并行，不会冲突。002 与 004 都改 `App.css` 的不同区段，
但**不要并行**——同文件相邻区域容易互相覆盖。

## 本次未排期的发现

体检一共确认了 9 条，上面只覆盖了 4 条。其余 5 条留在这里备查，没有写成方案：

| 严重度 | 位置 | 发现 |
| --- | --- | --- |
| MEDIUM | `desktop/src/components/Chat.tsx:130-137`、`desktop/src/App.tsx:934-951` | 点侧栏一行，消息列表连输入框整块被卸载换成详情面板；记忆详情↔知识库详情之间也是硬切。这是「几十次/天」的导航，动效必须压在 150ms 内 |
| LOW | `desktop/src/components/SideList.tsx:225-227` | 待补全行的 key 是 `` `${index}-${q}` ``，删一行会让后面所有行 key 变化、整片重挂。**给这个列表加任何进场动效之前必须先换成稳定 key**，否则动画会在没变的行上乱放 |
| LOW | `desktop/src/App.css:479-498` | `.qa-row-del` 靠 `opacity: 0 → 1` 在行 hover 时露出，无过渡。行 hover 是高频面，只配约 100ms 的淡入，不是标准时长 |
| LOW | `desktop/src/App.css:839`、`:113`、`:959` | `.dot.ok` 用字面量 `#3ecf8e`，兄弟规则却用 `var(--dot-idle)` / `var(--danger)`；另有两处 `color: #fff` 绕过了已有的 `--primary-fg` |
| LOW | `desktop/src/App.css` 全局 | 缺 `prefers-reduced-motion`。已并入 002，此处只作记录 |

另有四处「本该有动效」的接缝（附加项，非缺陷）：思考步骤逐条追加与最终答案整块出现
（`Chat.tsx:164-182`、`App.tsx:671-676`，步骤到达很快，只能用 transition / `@starting-style`，
不能用 keyframes）；`mode-bar` 切「记忆/提炼」时凭空出现并把对话区顶下去
（`Chat.tsx:142-149`，注意别去动画 `height`）；`{n} 条新` / `{n} 条改` 徽标在 5 秒轮询里跳出来
（`SideList.tsx:120-139`，只做首次出现的淡入，数字本身不能动）；知识库抓取只有一次文字替换
（`SideList.tsx:184-186`）。

## 体检里被驳掉的项（别再报一遍）

以下都在 `desktop/src/` 里核过、确认不存在或不适用，不要当成待办：

- `transition: all`、`ease-in`、`scale(0)`、Framer Motion 简写属性、父级
  `setProperty('--x')` 驱动子元素、`requestAnimationFrame` 循环、`filter: blur`、
  `backdrop-filter`、动画化布局属性——一处都没有。
- `transform-origin` 相关发现不成立：六个浮层全是视口居中的 modal
  （`App.css:852-861` flex 居中），按 AUDIT 第 3 节居中 modal 本就豁免。
  下拉全是原生 `<select>`（`Chat.tsx:80`、`ToolBar.tsx:33`、`SideList.tsx:202`），
  由系统绘制、动不了。
- `@media (hover: hover) and (pointer: fine)` 门控在 Tauri 桌面端不适用，
  且现有 hover 规则没有一条改 `transform`。
- 思考步骤的 `key={i}`（`Chat.tsx:164-166`）是只追加、不重排的列表，索引 key 不会破坏进场动画。
- 主题切换整套色板一帧切换（`desktop/src/theme.ts:28-29`）是有意为之；给全局元素加
  颜色过渡意味着每个元素都要过渡，代价远大于收益，不要做。
