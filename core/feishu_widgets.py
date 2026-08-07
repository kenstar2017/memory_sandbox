"""读飞书文档里那些「不在正文里」的组件。

docx 的 raw_content 只收文字块。实测（828 块的真实技术方案文档）：
- 文档小组件 add_ons（mermaid 时序图等）的源码**会**进 raw_content，不用管；
- 画板 board、图片 image、电子表格 sheet 这些块在正文里没有任何痕迹，
  载荷里只留一个 token，内容是另一份独立资源。

这里负责把「另一份资源」捞回来拼成文字附录。目前只有画板真读，其余给出显式
占位——让调用方知道「这里有东西没读到」，比静默丢掉强得多。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# docx block_type。别照着「块类型对照表」的记忆写，容易记岔（40 是 add_ons 不是
# OKR 进展，43 是 board 不是议程），以块载荷的字段名为准。
BLOCK_BITABLE = 18
BLOCK_IMAGE = 27
BLOCK_MINDNOTE = 29
BLOCK_SHEET = 30
BLOCK_ADD_ONS = 40
BLOCK_BOARD = 43

# 载荷字段名 -> (block_type, 人话名字)
_WIDGET_KINDS: Dict[int, Tuple[str, str]] = {
    BLOCK_BOARD: ("board", "画板"),
    BLOCK_SHEET: ("sheet", "电子表格"),
    BLOCK_BITABLE: ("bitable", "多维表格"),
    BLOCK_MINDNOTE: ("mindnote", "思维笔记"),
    BLOCK_IMAGE: ("image", "图片"),
}

# 读不到内容时的说明。写清「缺什么」，用户才知道下一步该开什么权限
_NOT_SUPPORTED = {
    "sheet": "内容需调用电子表格接口（sheets:spreadsheet:readonly），暂未接入",
    "bitable": "内容需调用多维表格接口（bitable:app:readonly），暂未接入",
    "mindnote": "思维笔记暂无开放读取接口",
    "image": "图片内容需下载媒体文件后做 OCR，暂未接入",
}

BOARD_SCOPE = "board:whiteboard:node:read"

# 单个画板最多列这么多图形。画板可以有上千节点，全塞进正文会挤爆上下文
MAX_NODES_PER_BOARD = 200


@dataclass
class WidgetRef:
    """正文之外的一个组件块。"""

    block_id: str
    block_type: int
    kind: str
    label: str
    token: str


def collect_widgets(blocks: Sequence[dict]) -> List[WidgetRef]:
    """从文档块列表里挑出带独立资源的组件块，按文档顺序返回。"""
    out: List[WidgetRef] = []
    for b in blocks or []:
        bt = b.get("block_type")
        if bt not in _WIDGET_KINDS:
            continue
        kind, label = _WIDGET_KINDS[bt]
        payload = b.get(kind) if isinstance(b.get(kind), dict) else {}
        # 载荷字段名和 kind 不一定同名（sheet 块的字段是 sheet，图片是 image），
        # 取不到就退回「扫所有非元信息字段找 token」
        token = str((payload or {}).get("token") or "")
        if not token:
            for k, v in b.items():
                if isinstance(v, dict) and v.get("token"):
                    token = str(v["token"])
                    break
        out.append(
            WidgetRef(
                block_id=str(b.get("block_id") or ""),
                block_type=int(bt),
                kind=kind,
                label=label,
                token=token,
            )
        )
    return out


def _node_text(node: dict) -> str:
    """图形里的文字：优先 text.text，其次富文本段落。"""
    plain = str(((node.get("text") or {}).get("text")) or "").strip()
    if plain:
        return plain
    parts: List[str] = []
    for para in ((node.get("rich_text") or {}).get("paragraphs")) or []:
        for el in para.get("elements") or []:
            for key in ("text_element", "link_element"):
                sub = el.get(key) or {}
                if sub.get("text"):
                    parts.append(str(sub["text"]))
    return "".join(parts).strip()


def _one_line(text: str) -> str:
    """压成一行：画板上竖排的两行字，取出来是带 \\n 的一个标签。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _connector_caption(node: dict) -> str:
    """连线上的文字，如流程图里的「是 / 否」。"""
    caps = (node.get("connector") or {}).get("captions") or {}
    text = "".join(str(d.get("text") or "") for d in (caps.get("data") or []))
    return _one_line(text)


def _sort_key(node: dict) -> Tuple[float, float]:
    """从上到下、从左到右——画板没有阅读顺序，只能靠坐标近似。"""

    def _num(v: object) -> float:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    return (_num(node.get("y")), _num(node.get("x")))


def render_board(nodes: Sequence[dict]) -> str:
    """把画板节点数组渲染成缩进文本 + 连线列表。"""
    nodes = list(nodes or [])
    if not nodes:
        return "（空画板）"

    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    connectors = [n for n in nodes if n.get("type") == "connector"]
    shapes = [n for n in nodes if n.get("type") != "connector"]

    # 图形文字常带换行（画板上是两行摆放的一个词）。必须在这里压平：连线渲染
    # 直接拼 label，不压的话「A --是--> B」会被断成两行，整段连线列表读不成句
    labels: Dict[str, str] = {}
    for n in shapes:
        labels[str(n.get("id") or "")] = _one_line(_node_text(n))

    children: Dict[str, List[dict]] = {}
    roots: List[dict] = []
    for n in shapes:
        parent = str(n.get("parent_id") or "")
        if parent and parent in by_id:
            children.setdefault(parent, []).append(n)
        else:
            roots.append(n)

    lines: List[str] = []
    blank = 0
    seen: set = set()
    budget = MAX_NODES_PER_BOARD

    def walk(node: dict, depth: int) -> None:
        nonlocal blank, budget
        nid = str(node.get("id") or "")
        if nid in seen or budget <= 0:
            return
        seen.add(nid)
        text = labels.get(nid, "")
        if text:
            budget -= 1
            lines.append("  " * depth + "- " + text)
        else:
            blank += 1
        kids = sorted(children.get(nid, []), key=_sort_key)
        for kid in kids:
            # 无文字的容器不占缩进层级，否则一堆 group 会把树推得很深
            walk(kid, depth + 1 if text else depth)

    for root in sorted(roots, key=_sort_key):
        walk(root, 0)
    # 兜底：父子成环、或父节点指向连线时上面一轮会一个都走不到，
    # 那样整张画板会静默变成空白——宁可丢层级，也不能丢内容
    for node in sorted(shapes, key=_sort_key):
        walk(node, 0)

    out: List[str] = []
    if lines:
        out.extend(lines)
    if budget <= 0:
        out.append(f"…（图形过多，只列了前 {MAX_NODES_PER_BOARD} 个）")
    if blank:
        out.append(f"（另有 {blank} 个无文字图形，如箭头、底框）")

    def endpoint(obj: object) -> str:
        """连线端点未必挂在图形上：也可以直接钉在画布坐标上。"""
        eid = str((obj or {}).get("id") or "") if isinstance(obj, dict) else ""
        if not eid:
            return "（空白处）"
        if eid not in by_id:
            return "（未知图形）"
        return labels.get(eid) or "（无文字图形）"

    edges: List[str] = []
    for c in sorted(connectors, key=_sort_key):
        conn = c.get("connector") or {}
        src = endpoint(conn.get("start_object"))
        dst = endpoint(conn.get("end_object"))
        if not labels.get(str((conn.get("start_object") or {}).get("id") or "")) and not labels.get(
            str((conn.get("end_object") or {}).get("id") or "")
        ):
            # 两头都没文字的连线说明不了任何事，列出来只是噪音
            continue
        cap = _connector_caption(c)
        arrow = f" --{cap}--> " if cap else " → "
        edges.append(f"- {src}{arrow}{dst}")
    if edges:
        out.append("连线：")
        out.extend(edges)

    return "\n".join(out) if out else "（画板里没有文字）"


def read_board_nodes(
    api_base: str,
    access_token: str,
    whiteboard_id: str,
    timeout: float,
) -> List[dict]:
    """拉画板全部节点。该接口一次返回全量，没有分页。"""
    from .feishu import _http_json

    path = urllib.parse.quote(whiteboard_id, safe="")
    url = f"{api_base}/open-apis/board/v1/whiteboards/{path}/nodes"
    data = _http_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"读画板失败: {data.get('msg') or data}")
    return list((data.get("data") or {}).get("nodes") or [])


def _board_error_hint(err: str) -> str:
    """把画板读取的报错翻译成「下一步做什么」。"""
    low = err.lower()
    if "2890005" in err or "forbidden" in low:
        return "当前身份没有这个画板的阅读权限（画板可能来自他人文档）"
    if "20027" in err or "permission" in low or "scope" in low or "99991672" in err:
        return (
            f"应用缺少画板读权限：请在开放平台开通「查看画板节点（{BOARD_SCOPE}）」，"
            "再运行 python3 scripts/feishu_login.py 重新授权"
        )
    return err


def widget_appendix(
    api_base: str,
    access_token: str,
    blocks: Sequence[dict],
    timeout: float,
    *,
    max_chars: int = 20000,
) -> str:
    """
    给文档正文拼一段组件附录；没有组件时返回空串。

    单个组件读失败只影响它自己那一段——正文和别的组件照常返回。文档里有东西
    读不到是常态（权限、类型不支持），把原因写进附录比整篇读取失败有用。
    """
    widgets = collect_widgets(blocks)
    if not widgets:
        return ""

    counts: Dict[str, int] = {}
    for w in widgets:
        counts[w.label] = counts.get(w.label, 0) + 1
    summary = "、".join(f"{n} 个{label}" for label, n in counts.items())

    chunks: List[str] = [f"【文档组件附录】正文之外还有 {summary}："]
    seq: Dict[str, int] = {}
    cache: Dict[str, str] = {}
    for w in widgets:
        seq[w.kind] = seq.get(w.kind, 0) + 1
        head = f"── {w.label} {seq[w.kind]} ──"
        if w.kind != "board":
            chunks.append(f"{head}\n（未读取：{_NOT_SUPPORTED.get(w.kind, '暂不支持')}）")
            continue
        if not w.token:
            chunks.append(f"{head}\n（未读取：块里没有画板 token）")
            continue
        if w.token in cache:  # 同一画板被插入多次时不重复请求
            chunks.append(f"{head}\n{cache[w.token]}")
            continue
        try:
            body = render_board(
                read_board_nodes(api_base, access_token, w.token, timeout)
            )
        except Exception as e:  # noqa: BLE001
            body = f"（读取失败：{_board_error_hint(str(e))}）"
        cache[w.token] = body
        chunks.append(f"{head}\n{body}")

    text = "\n\n".join(chunks)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（组件附录过长已截断）"
    return text
