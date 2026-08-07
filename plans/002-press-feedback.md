# 002 — 给按钮加按压反馈，并补齐缺失的 hover

- **Status**: TODO
- **Depends on**: 001（要用 `--ease-out` / `--dur-press` / `--dur-hover` / `--press-scale`）
- **Commit**: 71cbfdd
- **Severity**: MEDIUM
- **Category**: 3. Physicality & origin（附带 6. Accessibility）
- **Estimated scope**: 1 个文件（`desktop/src/App.css`），约 +45 行

## Problem

三簇最高频的控件**既没有 `:hover` 也没有 `:active`**，点下去界面毫无反应，
按 AUDIT.md 第 3 节「可按压元素必须有按压反馈」这是硬伤；而另外两簇同样外观的按钮
却有 hover，导致同一个视觉族里反馈不一致。

没有任何反馈的（共用同一条基线规则）：

```css
/* desktop/src/App.css:681-692 — 当前：只有静态外观，没有 :hover 也没有 :active */
.toolbar-actions button,
.mode-bar button,
.composer button,
.modal-actions button {
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}

/* desktop/src/App.css:694-698 — 当前：主按钮同样没有任何状态 */
button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--primary-fg);
}
```

落在这条规则下的具体元素：

- 顶栏三个按钮「记忆」「飞书机器人」「配置」——`desktop/src/components/Chat.tsx:93-118`
- 输入区「发送」主按钮——`desktop/src/components/Chat.tsx:218`
- 所有弹窗底部按钮，含确认弹窗的主按钮——`desktop/src/components/DialogHost.tsx:48-59`、
  `desktop/src/components/ConfigModal.tsx:119-132`、`desktop/src/components/FeishuBotModal.tsx:145-173`、
  `desktop/src/components/CursorHooksModal.tsx:136-158`、`desktop/src/components/RetrievalModal.tsx:104-125`、
  `desktop/src/components/AnswerModal.tsx:134-158`
- 「取消」按钮（mode-bar）——`desktop/src/components/Chat.tsx:145-147`

侧栏三个 tab 同样只有静态外观（它是「几十次/天」的主导航）：

```css
/* desktop/src/App.css:126-141 — 当前 */
.side-tabs button {
  flex: 1;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
  cursor: pointer;
}

.side-tabs button.active {
  color: var(--text);
  border-color: var(--accent);
  background: var(--accent-soft);
}
```

对应元素：`desktop/src/components/SideList.tsx:141-163`（待补全 / 已记住 / 知识库）。

而这两簇**有** hover，是本次要照着抄的范例，也是不一致的证据：

```css
/* desktop/src/App.css:294-296 — 当前（范例） */
.tool-actions button:hover {
  border-color: var(--accent-dim);
}

/* desktop/src/App.css:543-545 — 当前（范例） */
.detail-actions button:hover {
  border-color: var(--accent);
}
```

另外全局没有 `@media (prefers-reduced-motion: reduce)`。今天什么都不动所以还不算缺陷，
但本方案是第一次引入基于 `transform` 的位移，必须同批补上，否则就留下一个无人负责的缺口。

## Target

一条共享的 `transition` + 一条共享的 `:active` 缩放，覆盖上面所有按钮；
外加把缺失的 hover 按现有范例补齐。

```css
/* target — 追加在 desktop/src/App.css:698（button.primary 规则）之后 */

/* 按钮反馈：按下要有回应，否则界面像没听见 */
.toolbar-actions button,
.mode-bar button,
.composer button,
.modal-actions button,
.tool-actions button,
.detail-actions button,
.side-tabs button,
.side-new,
.side-add-doc button,
.side-backfill,
.theme-select,
button.ghost {
  transition:
    transform var(--dur-press) var(--ease-out),
    border-color var(--dur-hover) ease,
    background-color var(--dur-hover) ease,
    color var(--dur-hover) ease;
}

.toolbar-actions button:active:not(:disabled),
.mode-bar button:active:not(:disabled),
.composer button:active:not(:disabled),
.modal-actions button:active:not(:disabled),
.tool-actions button:active:not(:disabled),
.detail-actions button:active:not(:disabled),
.side-tabs button:active:not(:disabled),
.side-new:active,
.side-add-doc button:active:not(:disabled),
.side-backfill:active:not(:disabled),
button.ghost:active:not(:disabled) {
  transform: scale(var(--press-scale));
}

/* 补齐 hover：这几簇原先只有静态外观，和 .tool-actions button:hover 不一致 */
.toolbar-actions button:hover:not(:disabled),
.mode-bar button:hover:not(:disabled),
.modal-actions button:hover:not(:disabled),
.side-tabs button:hover:not(.active),
.theme-select:hover {
  border-color: var(--accent-dim);
}

.composer button:hover:not(:disabled),
button.primary:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}

/* 减弱动效：保留颜色与透明度反馈，只去掉位移（AUDIT 第 6 节：是更少更轻，不是归零） */
@media (prefers-reduced-motion: reduce) {
  :root {
    --press-scale: 1;
    --enter-scale: 1;
  }
}
```

`--accent-hover` 是新颜色 token，与主题相关，**必须深浅各加一份**：

```css
/* desktop/src/App.css:1-28 深色块内，紧跟 --accent-dim 之后 */
--accent-hover: #4f97fe;

/* desktop/src/App.css:30-56 浅色块内，紧跟 --accent-dim 之后 */
--accent-hover: #1d4ed8;
```

主按钮的 hover 不能复用 `--accent-dim`：它在深色里是更暗的 `#2456a0`、在浅色里是更亮的
`#93b4f5`，语义相反，拿来做 hover 会一个变暗一个变亮。

## Repo conventions to follow

- hover 只改颜色/边框，范例 `desktop/src/App.css:294-296`（`border-color: var(--accent-dim)`）。
  已有的 hover 规则没有一条改 `transform`，本方案也不要给 hover 加位移。
- 禁用态已经由 `desktop/src/App.css:700-703` 的 `button:disabled { opacity: 0.5; cursor: not-allowed; }`
  统一处理，所以所有交互态都要带 `:not(:disabled)`——「发送」按钮在 `busy` 时是禁用的
  （`desktop/src/components/Chat.tsx:218`），按下去不该缩放。
- `.side-new`（`App.css:106-114`）是 `<button>` 但从不禁用（`SideList.tsx:120-139` 只在有数时渲染），
  所以它的 `:active` 不需要 `:not(:disabled)`。
- 颜色 token 深浅成对出现，范例是 `App.css:8`（深 `--accent`）与 `App.css:36`（浅 `--accent`）。
- 已有多选择器共用一条规则的写法，范例 `App.css:681-684`，本方案沿用这种平铺列举而不是引入嵌套。
- 中文注释只解释「为什么」，范例 `App.css:143`、`:418`、`:478`。

## Steps

1. 确认 001 已完成：`grep -n -- "--press-scale" desktop/src/App.css` 必须命中一次。
   若没有，先执行 `plans/001-motion-tokens.md`，不要在本方案里补 token 定义。
2. 在 `desktop/src/App.css:9`（深色块的 `--accent-dim: #2456a0;`）之后插入
   `  --accent-hover: #4f97fe;`。
3. 在 `desktop/src/App.css:37`（浅色块的 `--accent-dim: #93b4f5;`）之后插入
   `  --accent-hover: #1d4ed8;`。
4. 在 `desktop/src/App.css:698`（`button.primary { … }` 的闭合大括号）之后，
   追加 Target 里的四段：共享 `transition`、共享 `:active`、补齐的 hover、
   `prefers-reduced-motion` 块。顺序照抄。
5. 不要改动 `App.css:294-296` 与 `App.css:543-545` 两条已有 hover——它们已经对了，
   新增的共享 `transition` 会自动让它们的换色也带上 120ms 过渡。
6. 不要碰任何 `.tsx`：本方案不需要改标记。

## Boundaries

- 只改 `desktop/src/App.css`。任何 `.tsx`、`.ts`、`app_web.py` 都不在范围内。
- 不要给 `:hover` 加 `transform`：hover 是「几十次/天」的高频面，AUDIT 第 1 节要求尽量克制。
- 不要给 `.qa-row`（`App.css:419-437`）和 `.qa-row-del`（`App.css:479-498`）加过渡——
  列表行悬停是另一条未排期的发现，混进来会让本次改动无法单独回滚。
- 不要用 `@media (hover: hover) and (pointer: fine)` 包住 hover：这是 Tauri 桌面应用
  （`desktop/src-tauri/`），只有精确指针，加了是死代码。
- 不要把 `prefers-reduced-motion` 写成 `transition-duration: 0.01ms !important` 全局清零那种写法——
  AUDIT 第 6 节明确要求保留有助理解的颜色/透明度过渡，只去掉位移。
- 不要新增依赖。
- 若引用的行号内容与上面不符（自 commit 71cbfdd 后有漂移），**停下报告**。

## Verification

- **Mechanical**：`cd desktop && npm run build` 成功。
- **Feel check**：`cd desktop && npm run dev`，然后：
  - 按住顶栏「配置」按钮不放，按钮应轻微缩小；松手弹回。缩放要小到「说不清哪里变了但确实有回应」，
    如果明显看出在缩小说明值改大了，检查是否用了 `--press-scale` 而不是手写数字。
  - 按住输入区「发送」按钮，同样缩小；把输入框清空让它变成禁用态，再按一次，
    **不应有任何缩放**（`:not(:disabled)` 生效）。
  - 悬停顶栏按钮、弹窗底部按钮、侧栏三个 tab，边框应在约 120ms 内染上 `--accent-dim`，
    不是瞬间跳变。
  - 悬停「发送」和任意主按钮，背景应变成更亮/更深一档的蓝，深浅两个主题都要试
    （顶栏主题下拉，`desktop/src/components/Chat.tsx:80`）：深色下应更亮，浅色下应更深。
    若两个主题里都变浅，说明误用了 `--accent-dim`。
  - 侧栏当前选中的那个 tab 悬停时**不应**变边框（`:not(.active)` 生效）。
  - DevTools → Animations 面板把播放速度调到 10%，再按一次按钮：缩小过程应是一开始就快、
    尾部收得慢（ease-out），不应有起步迟滞感。
  - DevTools → Rendering → 勾选 `prefers-reduced-motion: reduce`，重新按按钮：
    **缩放消失但 hover 换色仍然存在**。两者都消失就是写成了全局清零，不符合要求。
- **Done when**：
  - `grep -c ":active" desktop/src/App.css` 返回 ≥ 11。
  - `grep -c -- "--accent-hover" desktop/src/App.css` 返回 `4`（深浅各 1 处定义 + 2 处引用）。
  - `grep -n "prefers-reduced-motion" desktop/src/App.css` 命中 1 处。
  - 上面每一条 feel check 都亲眼确认过。
