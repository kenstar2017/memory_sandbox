"""在飞书文档里建画板，并往画板里画流程图。

飞书**没有**「独立画板文件」这种东西：官方文档写明 board 只有节点级 API，
画板永远是某篇文档里 `block_type=43` 的一个块，那个块载荷里的 token 就是 whiteboard_id。
所以这里的两件事是：

1. 建画板 = 往文档追加一个画板块（走已有的 docx 创建块接口，不需要新权限）；
2. 画内容 = 往 whiteboard_id 批量创建节点（board/v1 的节点接口，需要
   `board:whiteboard:node:create`，我们原先只申请了 `:read`）。

布局是纯函数（`flow_nodes`），不碰网络，方便单测；HTTP 那几步各自只做一件事。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .config import FeishuConfig
from .feishu import (
    FeishuDocRef,
    _docx_url,
    _http_json,
    _resolve_credentials,
    _resolve_document_id,
    _with_user_token,
    extract_feishu_urls,
    feishu_configured,
)
from .feishu_widgets import BLOCK_BOARD

NODE_CREATE_SCOPE = "board:whiteboard:node:create"

# 接口上限：一次最多 3000 个节点
MAX_NODES = 3000

# 画板画布坐标以 px 为单位，原点在画板中心附近。这套尺寸是照着飞书默认图形调的：
# 200×64 的圆角矩形放 14 号字大约能横排 12 个汉字，再长就该换行了
_BOX_W = 200.0
_BOX_H = 64.0
_GAP = 56.0

# 只放开常用的几种，避免调用方从几十个枚举里瞎猜。键是人话，值是接口枚举
SHAPES: Dict[str, str] = {
    "round_rect": "round_rect",
    "rect": "rect",
    "ellipse": "ellipse",
    "diamond": "diamond",
    "parallelogram": "parallelogram",
    # 别名，方便中文调用方
    "圆角矩形": "round_rect",
    "矩形": "rect",
    "椭圆": "ellipse",
    "菱形": "diamond",
    "平行四边形": "parallelogram",
}

DIRECTIONS = ("down", "right")


@dataclass
class FeishuBoardResult:
    ok: bool
    whiteboard_id: str = ""
    block_id: str = ""
    document_id: str = ""
    url: str = ""
    title: str = ""
    nodes_written: int = 0
    error: str = ""


def _text_payload(text: str, *, align: str = "center") -> dict:
    return {
        "text": text,
        "font_weight": "regular",
        "font_size": 14,
        "horizontal_align": align,
        "vertical_align": "mid",
        "text_color": "#1f2329",
    }


def _box_style() -> dict:
    return {
        "fill_color": "#e1eaff",
        "fill_opacity": 100,
        "border_width": "narrow",
        "border_color": "#4e83fd",
        "border_opacity": 100,
        "border_style": "solid",
    }


def _line_style() -> dict:
    return {
        "border_width": "narrow",
        "border_color": "#646a73",
        "border_opacity": 100,
        "border_style": "solid",
    }


def _endpoint(node_id: str, snap_to: str, px: float, py: float) -> Dict[str, object]:
    """
    连线的一端。字段名以接口的**字段表**为准：connector.start / end，里面套
    attached_object。文档里的「请求体示例」写的是 start_object / end_object，
    那份是错的——照示例发会被服务端拒成 4005072 connector info empty。
    """
    return {
        "attached_object": {
            "id": node_id,
            "snap_to": snap_to,
            "position": {"x": px, "y": py},
        }
    }


def flow_nodes(
    steps: Sequence[str],
    *,
    direction: str = "down",
    shape: str = "round_rect",
    edge_labels: Sequence[str] = (),
) -> List[dict]:
    """
    把一串步骤排成流程图节点（图形 + 连线），返回可直接提交的节点列表。

    纯函数：不读配置、不发请求。连线靠 `start_object` / `end_object` 引用图形的 id，
    所以每个图形都得自带 id——接口允许省略 id，但省了就没法连线。
    """
    texts = [str(s or "").strip() for s in steps]
    texts = [t for t in texts if t]
    if not texts:
        raise ValueError("步骤为空，画不出流程图")
    if direction not in DIRECTIONS:
        raise ValueError(f"direction 只能是 {' / '.join(DIRECTIONS)}，收到 {direction!r}")
    kind = SHAPES.get(shape)
    if not kind:
        raise ValueError(f"不支持的图形 {shape!r}，可选：{'、'.join(sorted(set(SHAPES.values())))}")
    too_long = next((t for t in texts if len(t) > 1024), "")
    if too_long:
        raise ValueError(f"单个步骤文字超过 1024 字（{len(too_long)} 字）")

    nodes: List[dict] = []
    for i, text in enumerate(texts):
        if direction == "down":
            x, y = 0.0, i * (_BOX_H + _GAP)
        else:
            x, y = i * (_BOX_W + _GAP), 0.0
        nodes.append(
            {
                "id": f"n{i}",
                "type": "composite_shape",
                "x": x,
                "y": y,
                "width": _BOX_W,
                "height": _BOX_H,
                "text": _text_payload(text),
                "style": _box_style(),
                "composite_shape": {"type": kind},
            }
        )

    labels = list(edge_labels)
    for i in range(len(texts) - 1):
        head = nodes[i]
        if direction == "down":
            start = _endpoint(f"n{i}", "bottom", 0.5, 1)
            end = _endpoint(f"n{i + 1}", "top", 0.5, 0)
            # 连线自己的几何默认是 0：靠 snap 也能连上，但显式给出这段间隙的
            # 起点与长度，连线标签才有地方落，导出图片时也不会挤成一个点
            geom = {
                "x": float(head["x"]) + _BOX_W / 2,
                "y": float(head["y"]) + _BOX_H,
                "width": 0.0,
                "height": _GAP,
            }
        else:
            start = _endpoint(f"n{i}", "right", 1, 0.5)
            end = _endpoint(f"n{i + 1}", "left", 0, 0.5)
            geom = {
                "x": float(head["x"]) + _BOX_W,
                "y": float(head["y"]) + _BOX_H / 2,
                "width": _GAP,
                "height": 0.0,
            }
        connector: Dict[str, object] = {"start": start, "end": end}
        connector["end"]["arrow_style"] = "line_arrow"
        caption = str(labels[i]).strip() if i < len(labels) and labels[i] else ""
        if caption:
            connector["captions"] = {"data": [_text_payload(caption)]}
        nodes.append(
            {
                "id": f"c{i}",
                "type": "connector",
                "style": _line_style(),
                "connector": connector,
                **geom,
            }
        )
    return nodes


def _permission_hint(err: str) -> str:
    """把画板写入的报错翻译成「下一步做什么」。"""
    low = err.lower()
    if "2890005" in err or "forbidden" in low:
        return "；当前身份没有这个画板的编辑权限（画板可能在别人的文档里）"
    if "2890003" in err:
        return "；whiteboard_id 不存在，确认它取自文档里 block_type=43 那个块的 token"
    if "99991679" in err:
        # 实测原话「应用未获取所需的用户授权」：后台已经勾了，缺的是重新授权。
        # scope 在授权那一刻固定进 token，之后勾多少项都不会追加，refresh 也只沿用旧的
        return (
            f"；后台权限够了但当前 token 里没有「{NODE_CREATE_SCOPE}」，"
            "scope 是授权时固定在 token 里的：重跑 python3 scripts/feishu_login.py 重新授权即可"
        )
    if "20027" in err or "99991672" in err or "permission" in low or "scope" in low:
        return (
            f"；往画板里写节点需要「{NODE_CREATE_SCOPE}」权限，"
            "在开放平台后台勾上这项用户身份权限后，重跑 python3 scripts/feishu_login.py"
        )
    return ""


def _append_board_block(
    api_base: str, token: str, document_id: str, timeout: float
) -> Tuple[str, str]:
    """
    往文档末尾追加一个画板块，返回 (block_id, whiteboard_id)。

    whiteboard_id 取块载荷 `board.token`，不是 block_id：两者都在响应里，长得也像，
    拿错的话后面写节点会报 2890003 record missing。
    """
    path = urllib.parse.quote(document_id, safe="")
    url = f"{api_base}/open-apis/docx/v1/documents/{path}/blocks/{path}/children"
    data = _http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {token}"},
        body={"index": -1, "children": [{"block_type": BLOCK_BOARD, "board": {}}]},
        timeout=timeout,
    )
    if data.get("code") != 0:
        raise RuntimeError(
            f"创建画板块失败: [{data.get('code')}] {data.get('msg') or data}"
        )
    children = (data.get("data") or {}).get("children") or []
    if not children:
        raise RuntimeError(f"创建画板块成功但没返回块信息: {data}")
    block = children[0] or {}
    block_id = str(block.get("block_id") or "")
    whiteboard_id = str((block.get("board") or {}).get("token") or "")
    if not whiteboard_id:
        raise RuntimeError(
            f"画板块已创建（block_id={block_id}）但没拿到 board.token，无法往里画东西"
        )
    return block_id, whiteboard_id


def _post_nodes(
    api_base: str, token: str, whiteboard_id: str, nodes: List[dict], timeout: float
) -> int:
    """批量创建节点，返回接口确认创建的数量。"""
    path = urllib.parse.quote(whiteboard_id, safe="")
    url = f"{api_base}/open-apis/board/v1/whiteboards/{path}/nodes"
    data = _http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {token}"},
        body={"nodes": nodes},
        timeout=timeout,
    )
    if data.get("code") != 0:
        # 错误码必须带上：权限类报错有时走 HTTP 200 + code，此时 msg 里没有码，
        # _permission_hint 就认不出该提示重新授权还是该去后台开权限
        raise RuntimeError(
            f"写画板节点失败: [{data.get('code')}] {data.get('msg') or data}"
        )
    return len((data.get("data") or {}).get("ids") or [])


def _doc_ref(url: str) -> Optional[FeishuDocRef]:
    refs = extract_feishu_urls(url or "")
    return refs[0] if refs else None


def create_board(
    cfg: FeishuConfig,
    *,
    url: str = "",
    title: str = "",
    folder_token: str = "",
    steps: Sequence[str] = (),
    direction: str = "down",
    shape: str = "round_rect",
    edge_labels: Sequence[str] = (),
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> FeishuBoardResult:
    """
    建一个画板：往 `url` 指向的文档末尾追加画板块；没给 url 就先按 `title` 新建一篇文档。

    给了 `steps` 就顺手把流程图画上去，省掉「建完再查 whiteboard_id」这一步。
    画板块建出来之后画节点失败的，仍然返回 whiteboard_id 与链接——半成品必须让用户看得见。

    与其它飞书写操作同一门禁：confirmed 默认 False 且直接拒绝，不发任何请求。
    """
    if not confirmed:
        return FeishuBoardResult(
            ok=False,
            error="未确认：新建飞书画板需本人逐次确认，调用方须显式传 confirmed=True",
        )
    if not feishu_configured(cfg):
        return FeishuBoardResult(ok=False, error="未配置飞书 app_id / app_secret")
    if not (url or "").strip() and not (title or "").strip():
        return FeishuBoardResult(ok=False, error="要么给文档链接 url，要么给新建文档的 title")

    nodes: List[dict] = []
    if steps:
        try:
            nodes = flow_nodes(
                steps, direction=direction, shape=shape, edge_labels=edge_labels
            )
        except ValueError as e:
            return FeishuBoardResult(ok=False, error=str(e))
        if len(nodes) > MAX_NODES:
            return FeishuBoardResult(
                ok=False, error=f"节点数 {len(nodes)} 超过接口上限 {MAX_NODES}"
            )

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    document_id, doc_title = "", ""
    if (url or "").strip():
        ref = _doc_ref(url)
        if ref is None:
            return FeishuBoardResult(ok=False, error=f"这不像飞书文档链接: {url}")
        try:
            _tok, (document_id, doc_title) = _with_user_token(
                cfg,
                config_path,
                lambda t: _resolve_document_id(api_base, t, ref, timeout),
            )
        except Exception as e:  # noqa: BLE001
            return FeishuBoardResult(ok=False, error=f"定位文档失败: {e}")
    else:
        from .feishu import create_docx_document

        created = create_docx_document(
            cfg,
            title,
            folder_token=folder_token,
            config_path=config_path,
            confirmed=True,
        )
        if not created.ok:
            return FeishuBoardResult(ok=False, error=created.error)
        document_id, doc_title = created.document_id, created.title

    doc_url = _docx_url(cfg, document_id)
    try:
        _tok, (block_id, whiteboard_id) = _with_user_token(
            cfg,
            config_path,
            lambda t: _append_board_block(api_base, t, document_id, timeout),
        )
    except Exception as e:  # noqa: BLE001
        err = str(e)
        return FeishuBoardResult(
            ok=False,
            document_id=document_id,
            url=doc_url,
            title=doc_title,
            error=err + _permission_hint(err),
        )

    written = 0
    if nodes:
        try:
            _tok, written = _with_user_token(
                cfg,
                config_path,
                lambda t: _post_nodes(api_base, t, whiteboard_id, nodes, timeout),
            )
        except Exception as e:  # noqa: BLE001
            err = str(e)
            return FeishuBoardResult(
                ok=False,
                whiteboard_id=whiteboard_id,
                block_id=block_id,
                document_id=document_id,
                url=doc_url,
                title=doc_title,
                error=f"画板已建好但内容没画上：{err}{_permission_hint(err)}",
            )

    return FeishuBoardResult(
        ok=True,
        whiteboard_id=whiteboard_id,
        block_id=block_id,
        document_id=document_id,
        url=doc_url,
        title=doc_title,
        nodes_written=written,
    )


def draw_board_flow(
    cfg: FeishuConfig,
    whiteboard_id: str,
    steps: Sequence[str],
    *,
    direction: str = "down",
    shape: str = "round_rect",
    edge_labels: Sequence[str] = (),
    config_path: Optional[str] = None,
    confirmed: bool = False,
) -> FeishuBoardResult:
    """往已有画板里画一串流程图节点。节点是追加的，不会清掉画板上原有内容。"""
    if not confirmed:
        return FeishuBoardResult(
            ok=False,
            error="未确认：往飞书画板写内容需本人逐次确认，调用方须显式传 confirmed=True",
        )
    if not feishu_configured(cfg):
        return FeishuBoardResult(ok=False, error="未配置飞书 app_id / app_secret")
    board_id = (whiteboard_id or "").strip()
    if not board_id:
        return FeishuBoardResult(ok=False, error="缺少 whiteboard_id")
    try:
        nodes = flow_nodes(steps, direction=direction, shape=shape, edge_labels=edge_labels)
    except ValueError as e:
        return FeishuBoardResult(ok=False, error=str(e))
    if len(nodes) > MAX_NODES:
        return FeishuBoardResult(
            ok=False, error=f"节点数 {len(nodes)} 超过接口上限 {MAX_NODES}"
        )

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)
    try:
        _tok, written = _with_user_token(
            cfg,
            config_path,
            lambda t: _post_nodes(api_base, t, board_id, nodes, timeout),
        )
    except Exception as e:  # noqa: BLE001
        err = str(e)
        return FeishuBoardResult(
            ok=False, whiteboard_id=board_id, error=err + _permission_hint(err)
        )
    return FeishuBoardResult(ok=True, whiteboard_id=board_id, nodes_written=written)


def list_document_boards(
    cfg: FeishuConfig,
    url: str,
    *,
    config_path: Optional[str] = None,
) -> Tuple[List[Dict[str, str]], str]:
    """
    列出一篇文档里的所有画板，返回 ([{block_id, whiteboard_id}], 错误)。

    只读。给了文档链接却不知道 whiteboard_id 时用它——whiteboard_id 在界面上看不见。
    """
    if not feishu_configured(cfg):
        return [], "未配置飞书 app_id / app_secret"
    ref = _doc_ref(url)
    if ref is None:
        return [], f"这不像飞书文档链接: {url}"
    from .feishu import _all_blocks

    timeout = float(cfg.timeout or 30)
    _, _, _, api_base = _resolve_credentials(cfg)

    def _read(token: str) -> List[dict]:
        document_id, _title = _resolve_document_id(api_base, token, ref, timeout)
        return _all_blocks(api_base, token, document_id, timeout)

    try:
        _tok, blocks = _with_user_token(cfg, config_path, _read)
    except Exception as e:  # noqa: BLE001
        return [], str(e)
    out: List[Dict[str, str]] = []
    for b in blocks:
        if b.get("block_type") != BLOCK_BOARD:
            continue
        out.append(
            {
                "block_id": str(b.get("block_id") or ""),
                "whiteboard_id": str((b.get("board") or {}).get("token") or ""),
            }
        )
    return out, ""
