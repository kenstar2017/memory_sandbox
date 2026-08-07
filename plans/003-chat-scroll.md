# 003 — 流式回答期间停掉 smooth 滚动，并别把用户从历史里拽回底部

- **Status**: TODO
- **Depends on**: 无（不需要 001 的 token）
- **Commit**: 71cbfdd
- **Severity**: HIGH
- **Category**: 1. Purpose & frequency（附带 5. Performance）
- **Estimated scope**: 1 个文件（`desktop/src/components/Chat.tsx`），约 +12 −3 行

## Problem

全 App 唯一存在的动效是一次平滑滚动，而它恰好落在 AUDIT.md 第 1 节说的
「绝不要动画的高频路径」上，而且时长由浏览器决定、调不了。

```tsx
/* desktop/src/components/Chat.tsx:61-64 — 当前 */
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])
```

问题出在 `messages` 的变化频率。发一次问，NDJSON 的每个 `progress` 事件都会
往思考卡片里追加一条步骤，也就是每个事件都会改一次 `messages`：

```tsx
/* desktop/src/App.tsx:645-654 — 当前 */
      await chatStream(text, (ev) => {
        if (ev.type === 'progress') {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === thinkId && m.role === 'think'
                ? { ...m, steps: [...m.steps, ev.message] }
                : m,
            ),
          )
          return
        }
```

于是一次提问里，那个浏览器控制的平滑滚动会被反复重新发起、上一次还没走完就被
重定向，结果是「一直在缓慢追赶、始终没停稳」。回答落地时又是一次（`desktop/src/App.tsx:671-676`
把完整答案一次性 push 进去）。按 AUDIT.md 第 1 节，这条路径每天要走上百次，正确答案是
**不动画**，而不是换个曲线。

第二个问题：这个 effect 无条件滚到底。用户往上翻着看历史时，只要来一个 `progress`
事件就会被拽回底部，翻不动。

## Target

- 流式进行中（`busy === true`）用 `behavior: 'auto'`，即瞬时跳到底，不产生动画。
- 空闲时（一轮结束）才允许一次 `behavior: 'smooth'`，这是「偶发」频次，允许有动效。
- 用户已经往上翻走（距底部超过 120px）时不再强行滚动，除非最新一条是用户自己刚发的消息。

```tsx
/* target — desktop/src/components/Chat.tsx:61-64 位置 */
  const bottomRef = useRef<HTMLDivElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const anchor = bottomRef.current
    const box = boxRef.current
    if (!anchor || !box) return
    // 用户往上翻着看历史时别把他拽回来；但自己刚发的消息一定要跟到底
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120
    const mine = messages[messages.length - 1]?.role === 'user'
    if (!nearBottom && !mine) return
    // 流式期间每个 progress 事件都会改 messages，平滑滚动会被反复重定向、永远停不稳
    anchor.scrollIntoView({ behavior: busy ? 'auto' : 'smooth' })
  }, [messages, busy])
```

配合把滚动容器挂上 ref（这是本方案唯一的标记改动）：

```tsx
/* target — desktop/src/components/Chat.tsx:150 */
      <div className="chat" ref={boxRef}>
```

`120` 这个阈值是「差不多一条消息的高度」，不需要做成 token——只有这一处用。

## Repo conventions to follow

- 组件里已经用 `useRef` + `useEffect` 管 DOM，范例就是当前的 `bottomRef`
  （`desktop/src/components/Chat.tsx:61-64`），照同样风格加第二个 ref。
- `busy` 已经是 `Chat` 的现成 prop，无需新增：定义在 `desktop/src/components/Chat.tsx` 的
  `Props` 里，使用处见 `:124`（传给 `ToolBar`）、`:201`（`textarea` 的 `disabled`）、
  `:218`（发送按钮的 `disabled`）。直接读它，不要新造 state，也不要改 `App.tsx`。
- 消息类型有 `role` 字段，取值含 `'user' | 'bot' | 'sys' | 'meta' | 'think'`，
  判别写法见 `desktop/src/components/Chat.tsx:152`（`m.role === 'think'`）与 `:189`。
- 中文注释只解释「为什么」，范例 `desktop/src/components/Chat.tsx:73`、`:112`。

## Steps

1. 打开 `desktop/src/components/Chat.tsx`，确认第 61-64 行与上面 Problem 里引用的完全一致。
   不一致就停下报告。
2. 在 `const bottomRef = useRef<HTMLDivElement>(null)` 下面加一行
   `const boxRef = useRef<HTMLDivElement>(null)`。
3. 把第 62-64 行的 `useEffect` 整体替换为 Target 里那段，含两条中文注释。
4. 找到第 150 行 `<div className="chat">`，改为 `<div className="chat" ref={boxRef}>`。
   不要改这个 div 里的任何内容，也不要动第 196 行的 `<div ref={bottomRef} />` 哨兵。
5. 确认 `busy` 已在组件参数里解构（`desktop/src/components/Chat.tsx` 顶部的解构列表，
   当前含 `busy`）。若不在，停下报告——不要自行给组件加 prop。

## Boundaries

- 只改 `desktop/src/components/Chat.tsx`。**不要动 `desktop/src/App.tsx`**：
  流式回调的结构（`App.tsx:645-709`）不在本方案范围内，改它会牵动整条发送链路。
- 不要引入 `scroll-behavior: smooth` 一类 CSS，也不要改 `desktop/src/App.css:717-722`
  的 `.chat` 规则——用 CSS 做等于把这个判断权交回浏览器，正是要解决的问题。
- 不要改 `messages` 的数据结构，不要给消息加字段。
- 不要新增依赖，不要引入 `react-virtuoso` 之类的滚动库。
- 不要顺手做消息列表虚拟化：`{messages.map(...)}`（`Chat.tsx:151-195`）每次 progress
  都全量重渲染确有成本，但那是独立议题，未排期。
- 若行号内容与上面不符（自 commit 71cbfdd 后有漂移），**停下报告**。

## Verification

- **Mechanical**：`cd desktop && npm run build`（`tsc -b && vite build`）必须通过。
  新增 ref 有类型，`boxRef` 用在 `<div>` 上类型应当自洽；报类型错就是 ref 类型写错了。
- **Feel check**：`cd desktop && npm run dev`，本机 API 要在跑（见 `README_zh.md` 的快速开始）：
  - 问一个需要走大模型的问题，盯住思考卡片逐条追加的过程：视图应当**每次瞬时贴底**，
    不应看到那种「缓慢追赶、始终差一点」的滚动。
  - 一轮回答结束后再发一条短消息，这一次允许看到一次平滑滚动，且它应当能走完、停稳。
  - 回答生成中把聊天区往上滚到能看见早前的消息：**不应**再被拽回底部；
    此时继续等待，直到回答落地，视图仍应停在你滚到的位置。
  - 在上一步「已经滚上去」的状态下，直接在输入框发一条新消息：这一次**应当**跟到底部
    （最新一条是自己发的）。
  - DevTools → Performance 录一次完整提问：流式期间不应出现连续的 scroll 动画帧。
- **Done when**：
  - `grep -n "behavior: busy" desktop/src/components/Chat.tsx` 命中 1 处。
  - `grep -n "ref={boxRef}" desktop/src/components/Chat.tsx` 命中 1 处。
  - `grep -c "behavior: 'smooth'" desktop/src/components/Chat.tsx` 返回 `0`（不再有硬编码的 smooth）。
  - 上面五条 feel check 全部亲眼确认。
