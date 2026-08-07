# 004 — 给六个浮层加进场动效（只做进场，不做退场）

- **Status**: TODO
- **Depends on**: 001（要用 `--ease-out` / `--dur-enter` / `--enter-scale`）、
  002（`prefers-reduced-motion` 块在 002 里建立，本方案复用同一个块里的 `--enter-scale: 1`）
- **Commit**: 71cbfdd
- **Severity**: MEDIUM
- **Category**: 4. Interruptibility（附带 8. Missed opportunities）
- **Estimated scope**: 1 个文件（`desktop/src/App.css`），约 +16 行

## Problem

六个浮层全部靠条件卸载出现和消失，遮罩连面板在一帧内整块闪出，没有任何进场或退场：

```tsx
/* 当前 —— 六处一模一样的模式 */
desktop/src/components/DialogHost.tsx:33        if (!pending) return null
desktop/src/components/AnswerModal.tsx:52       if (!open || !seed) return null
desktop/src/components/ConfigModal.tsx:34       if (!open) return null
desktop/src/components/RetrievalModal.tsx:49    if (!open) return null
desktop/src/components/FeishuBotModal.tsx:63    if (!open) return null
desktop/src/components/CursorHooksModal.tsx:46  if (!open) return null
```

它们共用同一套 CSS，目前完全静态：

```css
/* desktop/src/App.css:852-861 — 当前 */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: var(--overlay);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
  padding: 24px;
}

/* desktop/src/App.css:863-875 — 当前 */
.modal {
  width: min(640px, 100%);
  max-height: min(90vh, 800px);
  overflow: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  color: var(--text);
}
```

按 AUDIT.md 第 1 节，弹窗属于「偶发」频次，正是允许标准动效的档位；
第 8 节也把「凭空出现/消失」列为该修的接缝。

## Target

进场用 `@starting-style`：遮罩淡入，面板同时淡入并从 `scale(0.96)` 放大到 1。
`@starting-style` 是纯 CSS 进场，不需要改任何 `.tsx`、不需要 `useEffect` 里
设 `mounted` 标记，也不需要新依赖。

```css
/* target — 追加在 desktop/src/App.css:875（.modal 规则闭合）之后 */

/* 六个浮层都是条件卸载出来的，用 @starting-style 做纯 CSS 进场；
   不支持的 webview 会直接显示，退化成现在的行为，不会坏 */
.modal-backdrop {
  transition: opacity var(--dur-enter) var(--ease-out);

  @starting-style {
    opacity: 0;
  }
}

.modal {
  transition:
    opacity var(--dur-enter) var(--ease-out),
    transform var(--dur-enter) var(--ease-out);

  @starting-style {
    opacity: 0;
    transform: scale(var(--enter-scale));
  }
}
```

三条不能改的取值约束：

1. **起点必须是 `scale(0.96)`（即 `--enter-scale`），绝不能是 `scale(0)`**。
   AUDIT.md 第 3 节：现实里没有东西从「无」里冒出来，起点要在 0.9–0.97 之间。
2. **不要设 `transform-origin`**。六个浮层都是视口居中的 modal
   （`.modal-backdrop` 是 `position: fixed` + flex 居中，见 `App.css:852-861`），
   AUDIT.md 第 3 节明确写了**居中 modal 豁免**「从触发点放大」这条规则，默认的
   `transform-origin: center` 在这里就是对的。谁看到这段想改成 `var(--transform-origin)`，
   那是把一条豁免项当成缺陷。
3. **只用 `transition`，不要用 `@keyframes`**。AUDIT.md 第 4 节：确认弹窗可以叠在
   已打开的弹窗上（`desktop/src/components/ConfigModal.tsx:38-47` 脏数据关闭时会弹确认），
   keyframes 被打断会从零重启，transition 才能中途重定向。

## 明确不做退场，以及为什么

- 条件卸载（`return null`）意味着元素在退场那一帧已经从 DOM 上消失，CSS 拿不到退场时机。
  要做退场就得把 open 状态提到父层、退场结束后再卸载，那是六个组件加 `App.tsx` 的状态改造，
  远超「动效」范围。
- 更重要的是：确认弹窗是用键盘关的——`desktop/src/components/DialogHost.tsx:16-31` 里
  Escape 关闭、Enter 确认。AUDIT.md 第 1 节要求键盘触发的动作**绝不加动画**。
  也就是说这里的退场本来就该瞬时，做了反而是错的。

所以本方案交付进场即止。这不是偷懒，是按规则的取舍。

## Repo conventions to follow

- 所有样式都在 `desktop/src/App.css` 一个文件里，按「组件区块」顺序排列；
  弹窗相关规则集中在 `App.css:852-960`，新增规则紧跟 `.modal`（`:863-875`）之后，
  不要放到文件末尾。
- 已有 CSS 嵌套用法可参考 `App.css:224`（`.side-backfill:hover:not(:disabled)` 是平铺写法）——
  本文件目前**没有**嵌套语法先例，但 `@starting-style` 嵌套在规则内是它的标准写法，
  可以引入；若执行时 `vite build` 报解析错误，改成非嵌套的
  `@starting-style { .modal-backdrop { opacity: 0; } }` 写法（等价）。
- 中文注释只解释「为什么」，范例 `App.css:143`、`:418`、`:881`。

## Steps

1. 确认 001 与 002 已完成：
   `grep -n -- "--enter-scale" desktop/src/App.css` 应命中 2 处（001 的定义 + 002 的
   reduced-motion 覆盖）。缺一个就先执行对应方案。
2. 定位 `desktop/src/App.css:875`，即 `.modal { … }` 规则的闭合大括号。
3. 在其后追加 Target 里的两段（`.modal-backdrop` 的过渡与 `@starting-style`、
   `.modal` 的过渡与 `@starting-style`），含中文注释。
4. 不要改 `App.css:852-875` 已有的两条规则本体，只追加新规则——保持「静态布局」与
   「动效」分开，方便回滚。
5. 不要动 `.confirm-modal`（`App.css:944-946`）和 `.modal-wide`（`App.css:877-879`）：
   它们只改宽度，会自动继承 `.modal` 的进场。
6. 六个 `.tsx` 一行都不要改。

## Boundaries

- 只改 `desktop/src/App.css`。六个浮层组件、`desktop/src/App.tsx`、`desktop/src/dialogs.ts`
  都不在范围内。
- 不做退场动效，不要为此把 `open` 状态提层、不要加 `setTimeout` 延迟卸载、
  不要引入 `react-transition-group` 一类库。
- 不要给 `.modal` 设 `transform-origin`（理由见 Target 第 2 条）。
- 不要给内容区（`.modal-hint`、`.retrieval-grid`、`.bot-log` 等）加逐项进场或 stagger：
  弹窗内部的加载态切换是另一条未排期的发现。
- 不要用 `@keyframes`。
- 不要新增依赖。
- 若行号内容与上面不符（自 commit 71cbfdd 后有漂移），**停下报告**。

## Verification

- **Mechanical**：`cd desktop && npm run build` 成功。若 CSS 嵌套写法导致构建失败，
  按「Repo conventions」里的非嵌套等价写法改写后重跑。
- **Feel check**：`cd desktop && npm run dev`，逐个打开六个浮层
  （顶栏「配置」「飞书机器人」；工具栏「检索设置」「AI 门禁」；侧栏点一条待补全出现的
  答案编辑弹窗；以及任意会弹确认的危险操作，如工具栏「清空长时」）：
  - 遮罩应在约 200ms 内淡入，面板同时淡入并轻微放大。整体应当「快到几乎察觉不到」，
    如果感觉慢，确认用的是 `--dur-enter`（200ms）而不是更大的值。
  - 面板**不应**从一个点冒出来（那说明 `--enter-scale` 被写成了 0 或很小的值），
    也不应从屏幕边缘飞进来（本方案不含位移）。
  - 面板应当从**正中央**放大，不是从触发按钮的位置——居中 modal 就该如此。
  - 在配置弹窗里改一个字符再点关闭，触发叠在上面的确认弹窗：第二层应当同样淡入，
    且底下那层不应重新播一次进场。
  - 快速连点「配置」开→关→开：不应看到动画从零重启后卡住的样子（transition 会中途重定向）。
  - DevTools → Animations 面板把播放速度调到 10%，再开一次弹窗：淡入与放大应同步开始、
    同步结束，不应一个先到一个后到。
  - DevTools → Rendering → 勾选 `prefers-reduced-motion: reduce`，再开一次：
    **淡入保留，放大消失**（`--enter-scale` 被 002 的 media 块覆盖为 1）。
    如果连淡入也没了，说明 002 里的 reduced-motion 块被写成了全局清零。
  - 关闭弹窗时应当是瞬时消失——这是本方案的预期结果，不是缺陷。
- **Done when**：
  - `grep -c "@starting-style" desktop/src/App.css` 返回 `2`。
  - `grep -n "transform-origin" desktop/src/App.css` 返回空（一处都不该有）。
  - `grep -c "@keyframes" desktop/src/App.css` 返回 `0`。
  - 上面每条 feel check 都亲眼确认过，六个浮层逐个试过。
