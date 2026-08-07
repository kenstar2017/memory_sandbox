"""文档组件（画板等）读取单测（无网络）。

样例数据的形状照着开放平台文档写：
- 文档块：block_type 43 = board，载荷 {"board": {"token": ...}}
- 画板节点：{"id","type","parent_id","children","x","y","text":{"text":...}}
  连线为 type=connector，端点在 connector.start_object.id / end_object.id，
  线上文字在 connector.captions.data[].text
"""

import unittest
from unittest import mock

from core.feishu_widgets import (
    BOARD_SCOPE,
    collect_widgets,
    render_board,
    widget_appendix,
)


def _board_block(token: str, block_id: str = "b1") -> dict:
    return {
        "block_id": block_id,
        "block_type": 43,
        "parent_id": "root",
        "board": {"token": token},
    }


def _shape(nid: str, text: str, *, x: float = 0, y: float = 0, parent: str = "") -> dict:
    return {
        "id": nid,
        "type": "composite_shape",
        "parent_id": parent,
        "x": x,
        "y": y,
        "text": {"text": text},
    }


def _edge(nid: str, src: str, dst: str, caption: str = "") -> dict:
    node = {
        "id": nid,
        "type": "connector",
        "x": 0,
        "y": 999,
        "connector": {"start_object": {"id": src}, "end_object": {"id": dst}},
    }
    if caption:
        node["connector"]["captions"] = {"data": [{"text": caption}]}
    return node


class CollectWidgetTests(unittest.TestCase):
    def test_finds_board_token(self):
        widgets = collect_widgets([{"block_type": 2, "text": {}}, _board_block("wb_1")])
        self.assertEqual(len(widgets), 1)
        self.assertEqual(widgets[0].kind, "board")
        self.assertEqual(widgets[0].token, "wb_1")

    def test_plain_text_blocks_are_not_widgets(self):
        blocks = [{"block_type": t} for t in (1, 2, 3, 12, 14, 31, 32, 34, 40)]
        self.assertEqual(collect_widgets(blocks), [])

    def test_add_ons_is_not_collected_because_it_rides_in_raw_content(self):
        """40 号块的 mermaid 源码本来就进正文，再读一遍等于重复。"""
        blocks = [{"block_type": 40, "add_ons": {"record": '{"data":"sequenceDiagram"}'}}]
        self.assertEqual(collect_widgets(blocks), [])

    def test_image_and_sheet_are_collected_for_visibility(self):
        blocks = [
            {"block_type": 27, "image": {"token": "img_1"}},
            {"block_type": 30, "sheet": {"token": "sh_1"}},
        ]
        kinds = [w.kind for w in collect_widgets(blocks)]
        self.assertEqual(kinds, ["image", "sheet"])

    def test_keeps_document_order(self):
        blocks = [
            {"block_type": 27, "image": {"token": "img_1"}},
            _board_block("wb_1"),
            {"block_type": 27, "image": {"token": "img_2"}},
        ]
        self.assertEqual([w.kind for w in collect_widgets(blocks)], ["image", "board", "image"])


class RenderBoardTests(unittest.TestCase):
    def test_lists_shapes_top_down(self):
        out = render_board([_shape("b", "第二步", y=100), _shape("a", "第一步", y=10)])
        self.assertLess(out.index("第一步"), out.index("第二步"))

    def test_left_to_right_within_a_row(self):
        out = render_board([_shape("r", "右", x=200, y=0), _shape("l", "左", x=10, y=0)])
        self.assertLess(out.index("左"), out.index("右"))

    def test_renders_edges_with_caption(self):
        nodes = [
            _shape("a", "提交审核"),
            _shape("b", "驳回", y=10),
            _edge("e", "a", "b", "不通过"),
        ]
        self.assertIn("- 提交审核 --不通过--> 驳回", render_board(nodes))

    def test_edge_without_caption_uses_arrow(self):
        nodes = [_shape("a", "开始"), _shape("b", "结束", y=10), _edge("e", "a", "b")]
        self.assertIn("- 开始 → 结束", render_board(nodes))

    def test_nesting_follows_parent_id(self):
        nodes = [_shape("g", "分区"), _shape("c", "区内图形", parent="g")]
        lines = [l for l in render_board(nodes).splitlines() if l.startswith((" ", "-"))]
        self.assertEqual(lines[0], "- 分区")
        self.assertEqual(lines[1], "  - 区内图形")

    def test_textless_container_does_not_add_indent(self):
        """一堆无文字的 group 不该把树越推越深。"""
        nodes = [
            {"id": "g", "type": "group", "parent_id": "", "x": 0, "y": 0},
            _shape("c", "内容", parent="g"),
        ]
        self.assertIn("- 内容", render_board(nodes).splitlines())

    def test_textless_shapes_are_counted_not_listed(self):
        nodes = [_shape("a", "有字"), _shape("b", ""), _shape("c", "")]
        out = render_board(nodes)
        self.assertIn("有字", out)
        self.assertIn("另有 2 个无文字图形", out)

    def test_rich_text_nodes_are_read(self):
        node = {
            "id": "a",
            "type": "sticky_note",
            "parent_id": "",
            "x": 0,
            "y": 0,
            "rich_text": {
                "paragraphs": [
                    {"elements": [{"text_element": {"text": "便签内容"}}]}
                ]
            },
        }
        self.assertIn("便签内容", render_board([node]))

    def test_dangling_endpoint_says_which_kind_of_nothing(self):
        """真实画板里常有连到空白处或连到无字图形的箭头，别都糊成一个 ?。"""
        blank = {
            "id": "e1",
            "type": "connector",
            "x": 0,
            "y": 0,
            "connector": {"start_object": {"id": "a"}, "end_object": {}},
        }
        nodes = [_shape("a", "开始"), _shape("b", ""), blank, _edge("e2", "a", "b")]
        out = render_board(nodes)
        self.assertIn("- 开始 → （空白处）", out)
        self.assertIn("- 开始 → （无文字图形）", out)

    def test_edge_between_two_textless_shapes_is_dropped(self):
        nodes = [_shape("a", ""), _shape("b", "", y=10), _edge("e", "a", "b")]
        self.assertNotIn("→", render_board(nodes))

    def test_multiline_labels_are_flattened(self):
        """画板上竖排两行的一个词，取出来带 \\n，不压平会把连线撕成两行。"""
        nodes = [
            _shape("a", "Lead\nReviewing"),
            _shape("b", "Compliance\nApproved", y=10),
            _edge("e", "a", "b", "lark\napproved"),
        ]
        out = render_board(nodes)
        self.assertIn("- Lead Reviewing", out)
        self.assertIn("- Lead Reviewing --lark approved--> Compliance Approved", out)
        for line in out.splitlines():
            self.assertNotIn("\n", line)

    def test_cycle_does_not_hang(self):
        a = _shape("a", "甲", parent="b")
        b = _shape("b", "乙", parent="a")
        self.assertIn("甲", render_board([a, b]))

    def test_huge_board_is_capped(self):
        nodes = [_shape(f"n{i}", f"图形{i}", y=i) for i in range(500)]
        out = render_board(nodes)
        self.assertIn("图形过多", out)
        self.assertLess(len(out.splitlines()), 260)

    def test_empty_board(self):
        self.assertEqual(render_board([]), "（空画板）")


class WidgetAppendixTests(unittest.TestCase):
    def test_no_widgets_no_appendix(self):
        self.assertEqual(
            widget_appendix("https://x", "tok", [{"block_type": 2}], 3.0), ""
        )

    def test_board_content_lands_in_appendix(self):
        nodes = [_shape("a", "PTE 审核"), _shape("b", "供应商回滚", y=10), _edge("e", "a", "b")]
        with mock.patch("core.feishu_widgets.read_board_nodes", return_value=nodes):
            out = widget_appendix("https://x", "tok", [_board_block("wb_1")], 3.0)
        self.assertIn("【文档组件附录】", out)
        self.assertIn("1 个画板", out)
        self.assertIn("PTE 审核", out)
        self.assertIn("PTE 审核 → 供应商回滚", out)

    def test_missing_scope_tells_user_what_to_enable(self):
        err = RuntimeError("读画板失败: 20027 no permission")
        with mock.patch("core.feishu_widgets.read_board_nodes", side_effect=err):
            out = widget_appendix("https://x", "tok", [_board_block("wb_1")], 3.0)
        self.assertIn(BOARD_SCOPE, out)
        self.assertIn("feishu_login.py", out)

    def test_forbidden_board_is_explained_not_swallowed(self):
        err = RuntimeError("读画板失败: 2890005 forbidden")
        with mock.patch("core.feishu_widgets.read_board_nodes", side_effect=err):
            out = widget_appendix("https://x", "tok", [_board_block("wb_1")], 3.0)
        self.assertIn("没有这个画板的阅读权限", out)

    def test_one_bad_board_does_not_kill_the_others(self):
        calls = {"n": 0}

        def _read(_base, _tok, whiteboard_id, _timeout):
            calls["n"] += 1
            if whiteboard_id == "bad":
                raise RuntimeError("boom")
            return [_shape("a", "好画板")]

        blocks = [_board_block("bad", "b1"), _board_block("good", "b2")]
        with mock.patch("core.feishu_widgets.read_board_nodes", side_effect=_read):
            out = widget_appendix("https://x", "tok", blocks, 3.0)
        self.assertIn("boom", out)
        self.assertIn("好画板", out)
        self.assertEqual(calls["n"], 2)

    def test_same_board_read_once(self):
        with mock.patch(
            "core.feishu_widgets.read_board_nodes", return_value=[_shape("a", "同一张")]
        ) as read:
            blocks = [_board_block("wb_1", "b1"), _board_block("wb_1", "b2")]
            out = widget_appendix("https://x", "tok", blocks, 3.0)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(out.count("同一张"), 2)

    def test_unsupported_widget_says_what_is_missing(self):
        blocks = [{"block_type": 30, "sheet": {"token": "sh_1"}}]
        out = widget_appendix("https://x", "tok", blocks, 3.0)
        self.assertIn("电子表格", out)
        self.assertIn("sheets:spreadsheet:readonly", out)

    def test_appendix_is_truncated(self):
        nodes = [_shape(f"n{i}", "长" * 50, y=i) for i in range(100)]
        with mock.patch("core.feishu_widgets.read_board_nodes", return_value=nodes):
            out = widget_appendix("https://x", "tok", [_board_block("wb_1")], 3.0, max_chars=500)
        self.assertLessEqual(len(out), 540)
        self.assertIn("截断", out)


class FetchIntegrationTests(unittest.TestCase):
    """fetch_feishu_document 里那条组件旁路。"""

    def _cfg(self):
        from core.config import FeishuConfig

        return FeishuConfig(
            enabled=True, app_id="a", app_secret="s", user_access_token="u-tok"
        )

    def _fetch(self, include_widgets: bool):
        from core.feishu import FeishuDocRef, fetch_feishu_document

        ref = FeishuDocRef(url="https://foo.feishu.cn/docx/Abc123", kind="docx", token="Abc123")
        with mock.patch("core.feishu._tenant_access_token", return_value="t-tok"), mock.patch(
            "core.feishu._docx_raw_content", return_value="正文内容"
        ), mock.patch("core.feishu._docx_title", return_value="标题"), mock.patch(
            "core.feishu.ensure_user_access_token", create=True
        ):
            return fetch_feishu_document(
                self._cfg(), ref, include_widgets=include_widgets
            )

    def test_off_by_default_does_not_list_blocks(self):
        with mock.patch("core.feishu._all_blocks") as all_blocks:
            res = self._fetch(include_widgets=False)
        all_blocks.assert_not_called()
        self.assertEqual(res.content, "正文内容")

    def test_widgets_appended_to_body(self):
        with mock.patch("core.feishu._all_blocks", return_value=[_board_block("wb_1")]), mock.patch(
            "core.feishu_widgets.read_board_nodes", return_value=[_shape("a", "画板里的字")]
        ):
            res = self._fetch(include_widgets=True)
        self.assertTrue(res.ok)
        self.assertTrue(res.content.startswith("正文内容"))
        self.assertIn("画板里的字", res.content)

    def test_block_listing_failure_still_returns_body(self):
        """组件读不到是常态，不该让整篇正文读失败。"""
        with mock.patch("core.feishu._all_blocks", side_effect=RuntimeError("列块炸了")):
            res = self._fetch(include_widgets=True)
        self.assertTrue(res.ok)
        self.assertIn("正文内容", res.content)
        self.assertIn("列块炸了", res.content)


class ScopeTests(unittest.TestCase):
    def test_board_scope_is_requested(self):
        from core.config import FeishuConfig
        from core.feishu_oauth import _merged_scopes

        merged = _merged_scopes(FeishuConfig(app_id="x", oauth_scope="wiki:node:read")).split()
        self.assertIn(BOARD_SCOPE, merged)


if __name__ == "__main__":
    unittest.main()
