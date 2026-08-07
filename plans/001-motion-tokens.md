# 001 — 建立 easing / duration token 地基

- **Status**: TODO
- **Commit**: 71cbfdd
- **Severity**: MEDIUM
- **Category**: 7. Cohesion & tokens
- **Estimated scope**: 1 个文件（`desktop/src/App.css`），约 +12 行

## Problem

`desktop/src/App.css` 里有一套完整的语义化颜色 token，深浅两套各 26 个、已核对无缺项，
但**没有任何 easing 或 duration token**。后续每一个动效方案都需要曲线和时长，没有地基就
只能各处手打 `cubic-bezier`，正是 AUDIT.md 第 7 节说的「五条几乎一样的 cubic-bezier」。

当前 token 结构（三块，注意第三块与主题无关）：

```css
/* desktop/src/App.css:1-28 — 深色（当前） */
:root,
html[data-theme='dark'] {
  --bg: #0f1419;
  --panel: #1a222c;
  /* …共 26 个颜色 token… */
  --primary-fg: #fff;
  color-scheme: dark;
}

/* desktop/src/App.css:30-56 — 浅色（当前）：同样 26 个 token 全量覆盖一遍 */
html[data-theme='light'] {
  --bg: #f4f6f9;
  /* … */
  color-scheme: light;
}

/* desktop/src/App.css:58-62 — 与主题无关的基础样式（当前） */
:root {
  font-family: 'SF Pro Text', 'PingFang SC', 'Helvetica Neue', sans-serif;
  color: var(--text);
  background: var(--bg);
}
```

## Target

动效 token 与主题无关（曲线和时长不随明暗变化），所以**只能加在 `:root` 那一块**
（当前 58-62 行），绝不能加进 1-28 / 30-56 两个主题块——加进去就要维护两份完全相同的值。

```css
/* target — desktop/src/App.css:58-62 那个 :root 块内追加 */
:root {
  font-family: 'SF Pro Text', 'PingFang SC', 'Helvetica Neue', sans-serif;
  color: var(--text);
  background: var(--bg);

  /* 动效：曲线与时长不随主题变化，所以放在这里而不是上面两个主题块 */
  --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
  --ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
  --dur-hover: 120ms;
  --dur-press: 160ms;
  --dur-enter: 200ms;

  /* 位移量做成变量，好让 prefers-reduced-motion 一处关掉（见 002 / 004） */
  --press-scale: 0.97;
  --enter-scale: 0.96;
}
```

这七个值都必须逐字照抄，不要凑近似值：

| Token | 值 | 依据 |
| --- | --- | --- |
| `--ease-out` | `cubic-bezier(0.23, 1, 0.32, 1)` | AUDIT.md 第 2 节，强化版 ease-out，用于进场/退场 |
| `--ease-in-out` | `cubic-bezier(0.77, 0, 0.175, 1)` | AUDIT.md 第 2 节，用于屏幕内位移/形变 |
| `--dur-hover` | `120ms` | 悬停/换色，AUDIT.md 第 2 节里 hover 属于最短档 |
| `--dur-press` | `160ms` | AUDIT.md 第 2 节按压反馈 100–160ms 的上限 |
| `--dur-enter` | `200ms` | AUDIT.md 第 2 节 modal 200–500ms 的下限，且满足「UI 动效不超过 300ms」 |
| `--press-scale` | `0.97` | AUDIT.md 第 3 节按压缩放 0.95–0.98 |
| `--enter-scale` | `0.96` | AUDIT.md 第 3 节进场起点 0.9–0.97，**绝不能是 0** |

不要加 `--ease-drawer`：本项目没有抽屉，加了就是没人用的死 token。

## Repo conventions to follow

- 所有 token 都是 CSS 自定义属性，写在 `desktop/src/App.css` 顶部，用短语义名
  （`--bg`、`--panel`、`--accent-soft`），不用 `--color-background-primary` 这种长名。
  新 token 沿用同样的短名风格。
- 主题相关的值必须深浅各写一份（`App.css:3-26` 与 `App.css:31-54` 是范例）；
  主题无关的值写在 `App.css:58-62` 的 `:root`（`font-family` 是范例）。
- 主题切换靠 `document.documentElement.dataset.theme`，见 `desktop/src/theme.ts:28`。
  动效 token 不参与主题，不需要动 `theme.ts`。
- 本文件用中文注释解释「为什么」，不解释「做了什么」（`App.css:91`、`:143`、`:418`、`:881`
  都是范例）。新增注释照这个风格。

## Steps

1. 打开 `desktop/src/App.css`，定位到第 58-62 行的 `:root { font-family: …; color: …; background: …; }`。
   确认它就是那个**不带** `html[data-theme=…]` 选择器的块——如果不是，停下报告。
2. 在 `background: var(--bg);` 之后、闭合大括号之前，追加上面 Target 里那两段（动效 token
   与两个 scale 变量），包含中文注释。值逐字照抄。
3. 不要改动 `App.css:1-28` 和 `App.css:30-56` 两个主题块的任何一行。
4. 不要在本方案里使用这些 token——消费它们是 002 与 004 的事。本方案交付的只有 token 定义。

## Boundaries

- 只改 `desktop/src/App.css`，且只在 58-62 行那个 `:root` 块内追加。
- 不要删改任何已有 token，不要顺手把 `App.css:839` 的 `#3ecf8e`、`:113` 与 `:959` 的 `#fff`
  收进 token——那是另一条未排期的发现，混进来会让本次改动无法单独回滚。
- 不要新增依赖，不要动 `desktop/package.json`。
- 不要写任何 `transition` / `@keyframes` / `:active` 规则。
- 若第 58-62 行的内容与上面引用的不一致（自 commit 71cbfdd 后有漂移），**停下报告**，不要自行猜测位置。

## Verification

- **Mechanical**：`cd desktop && npm run build`（等于 `tsc -b && vite build`）应当成功。
  纯 CSS 追加不会影响类型检查，若报错说明改错了文件。
- **Feel check**：本方案**不产生任何可见变化**。`cd desktop && npm run dev` 打开界面，
  确认深浅主题都和改动前完全一样（切顶栏主题下拉，`desktop/src/components/Chat.tsx:80`）。
  任何肉眼可见的差异都意味着改到了主题块。
- **Done when**：
  - `grep -c -- "--ease-out\|--ease-in-out\|--dur-hover\|--dur-press\|--dur-enter\|--press-scale\|--enter-scale" desktop/src/App.css` 返回 `7`。
  - `grep -n -- "--ease-out" desktop/src/App.css` 只有一处命中，且行号在 58-75 之间。
  - `npm run build` 通过，界面视觉零变化。
