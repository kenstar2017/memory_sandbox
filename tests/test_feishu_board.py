"""飞书画板：布局纯函数 + 建画板 / 画节点的写链路（不连网）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import FeishuConfig  # noqa: E402
from core.feishu_board import (  # noqa: E402
    MAX_NODES,
    NODE_CREATE_SCOPE,
    create_board,
    draw_board_flow,
    flow_nodes,
    list_document_boards,
)

BOARD_BLOCK_TYPE = 43


def cfg(**kw) -> FeishuConfig:
    return FeishuConfig(enabled=True, app_id="cli_x", app_secret="s", **kw)


class FlowLayoutTests(unittest.TestCase):
    def test_boxes_and_connectors_are_produced(self):
        nodes = flow_nodes(["一", "二", "三"])
        boxes = [n for n in nodes if n["type"] == "composite_shape"]
        lines = [n for n in nodes if n["type"] == "connector"]
        self.assertEqual(len(boxes), 3)
        self.assertEqual(len(lines), 2)

    def test_every_box_has_an_id_because_connectors_reference_it(self):
        # 接口允许省略 id，省了连线就无从指向，画出来是三个孤立方框
        nodes = flow_nodes(["一", "二"])
        ids = {n["id"] for n in nodes if n["type"] == "composite_shape"}
        conn = next(n for n in nodes if n["type"] == "connector")["connector"]
        self.assertIn(conn["start"]["attached_object"]["id"], ids)
        self.assertIn(conn["end"]["attached_object"]["id"], ids)

    def test_connector_endpoints_use_the_field_table_names_not_the_doc_example(self):
        """接口文档的请求示例写 start_object/end_object，是错的：服务端只认 start/end
        + attached_object，照示例发会被拒成 4005072 connector info empty。"""
        conn = next(n for n in flow_nodes(["一", "二"]) if n["type"] == "connector")[
            "connector"
        ]
        self.assertNotIn("start_object", conn)
        self.assertIn("attached_object", conn["start"])
        self.assertEqual(conn["end"]["arrow_style"], "line_arrow")

    def test_downward_layout_stacks_on_the_y_axis(self):
        boxes = [n for n in flow_nodes(["一", "二"], direction="down") if n["id"].startswith("n")]
        self.assertEqual(boxes[0]["x"], boxes[1]["x"])
        self.assertLess(boxes[0]["y"], boxes[1]["y"])

    def test_rightward_layout_stacks_on_the_x_axis(self):
        boxes = [n for n in flow_nodes(["一", "二"], direction="right") if n["id"].startswith("n")]
        self.assertEqual(boxes[0]["y"], boxes[1]["y"])
        self.assertLess(boxes[0]["x"], boxes[1]["x"])

    def test_edge_labels_land_on_the_matching_connector(self):
        nodes = flow_nodes(["一", "二", "三"], edge_labels=["", "是"])
        lines = [n for n in nodes if n["type"] == "connector"]
        self.assertNotIn("captions", lines[0]["connector"])
        self.assertEqual(
            lines[1]["connector"]["captions"]["data"][0]["text"], "是"
        )

    def test_connectors_span_the_gap_between_the_boxes_they_join(self):
        """连线几何默认 0：不给的话标签没地方落，导出图片时连线挤成一个点。"""
        nodes = flow_nodes(["一", "二"], direction="down")
        box, line = nodes[0], nodes[2]
        self.assertEqual(line["y"], box["y"] + box["height"])
        self.assertGreater(line["height"], 0)
        self.assertEqual(line["width"], 0)

    def test_blank_steps_are_dropped_not_drawn_as_empty_boxes(self):
        nodes = flow_nodes(["一", "   ", "二"])
        self.assertEqual(len([n for n in nodes if n["type"] == "composite_shape"]), 2)

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            flow_nodes([])

    def test_unknown_shape_is_rejected_with_the_options(self):
        with self.assertRaises(ValueError) as caught:
            flow_nodes(["一"], shape="八边形")
        self.assertIn("diamond", str(caught.exception))

    def test_chinese_shape_alias_works(self):
        nodes = flow_nodes(["判断"], shape="菱形")
        self.assertEqual(nodes[0]["composite_shape"]["type"], "diamond")


class CreateBoardTests(unittest.TestCase):
    def test_refuses_without_explicit_confirmation(self):
        with mock.patch("core.feishu_board._http_json") as http:
            res = create_board(cfg(), title="新画板")
        self.assertFalse(res.ok)
        self.assertIn("确认", res.error)
        http.assert_not_called()

    def test_needs_either_a_url_or_a_title(self):
        with mock.patch("core.feishu_board._http_json") as http:
            res = create_board(cfg(), confirmed=True)
        self.assertFalse(res.ok)
        http.assert_not_called()

    def test_bad_steps_are_caught_before_any_request(self):
        with mock.patch("core.feishu_board._http_json") as http:
            res = create_board(cfg(), title="x", steps=["一"], direction="斜着", confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("direction", res.error)
        http.assert_not_called()

    def _fake_calls(self, *, board_token="wb1", nodes_ok=True):
        calls = []

        def fake(method, url, *, headers=None, body=None, timeout=30.0):
            calls.append((url, body))
            if url.endswith("/children"):
                return {
                    "code": 0,
                    "data": {
                        "children": [
                            {
                                "block_id": "blk1",
                                "block_type": BOARD_BLOCK_TYPE,
                                "board": {"token": board_token},
                            }
                        ]
                    },
                }
            if "/board/v1/whiteboards/" in url:
                if not nodes_ok:
                    return {"code": 2890005, "msg": "forbidden"}
                sent = (body or {}).get("nodes") or []
                return {"code": 0, "data": {"ids": [f"i{i}" for i in range(len(sent))]}}
            raise AssertionError(f"没预料到的请求: {url}")

        return calls, fake

    def _patched(self, fake):
        return (
            mock.patch("core.feishu_board._http_json", side_effect=fake),
            mock.patch("core.feishu_oauth.ensure_user_access_token", return_value="u-tok"),
            mock.patch(
                "core.feishu_board._resolve_document_id", return_value=("doc1", "已有文档")
            ),
        )

    def test_inserts_a_board_block_into_an_existing_doc(self):
        calls, fake = self._fake_calls()
        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(
                cfg(doc_host="bytedance.larkoffice.com"),
                url="https://bytedance.larkoffice.com/docx/doc1",
                confirmed=True,
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.whiteboard_id, "wb1")
        self.assertEqual(res.block_id, "blk1")
        self.assertIn("/docx/doc1", res.url)
        body = calls[0][1]
        self.assertEqual(body["children"][0]["block_type"], BOARD_BLOCK_TYPE)

    def test_the_whiteboard_id_is_the_board_token_not_the_block_id(self):
        """拿错了后面写节点会报 2890003 record missing，而两个 id 长得一样。"""
        calls, fake = self._fake_calls(board_token="wb_real")
        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(cfg(), url="https://x.feishu.cn/docx/doc1", confirmed=True)
        self.assertEqual(res.whiteboard_id, "wb_real")
        self.assertNotEqual(res.whiteboard_id, res.block_id)

    def test_steps_are_drawn_right_after_the_board_appears(self):
        calls, fake = self._fake_calls()
        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(
                cfg(),
                url="https://x.feishu.cn/docx/doc1",
                steps=["一", "二"],
                confirmed=True,
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.nodes_written, 3)  # 两个方框 + 一根连线
        self.assertIn("/board/v1/whiteboards/wb1/nodes", calls[1][0])

    def test_a_half_built_board_still_reports_its_id(self):
        """画板建出来了、内容没画上：不把 id 交出去，用户既找不到它也不知道要清理。"""
        calls, fake = self._fake_calls(nodes_ok=False)
        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(
                cfg(),
                url="https://x.feishu.cn/docx/doc1",
                steps=["一", "二"],
                confirmed=True,
            )
        self.assertFalse(res.ok)
        self.assertEqual(res.whiteboard_id, "wb1")
        self.assertIn("画板已建好", res.error)

    def test_a_permission_error_names_the_scope_to_open(self):
        def fake(method, url, **kw):
            if url.endswith("/children"):
                return {
                    "code": 0,
                    "data": {"children": [{"block_id": "b", "board": {"token": "wb1"}}]},
                }
            return {"code": 99991672, "msg": "no permission"}

        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(
                cfg(), url="https://x.feishu.cn/docx/doc1", steps=["一", "二"], confirmed=True
            )
        self.assertIn(NODE_CREATE_SCOPE, res.error)
        self.assertIn("feishu_login", res.error)

    def test_a_stale_token_is_told_to_reauthorize_not_to_tick_the_console(self):
        """99991679 的原话是「应用未获取所需的用户授权」：后台已经勾了，缺的是重新授权。"""

        def fake(method, url, **kw):
            if url.endswith("/children"):
                return {
                    "code": 0,
                    "data": {"children": [{"block_id": "b", "board": {"token": "wb1"}}]},
                }
            return {"code": 99991679, "msg": "Unauthorized. required one of these privileges"}

        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(
                cfg(), url="https://x.feishu.cn/docx/doc1", steps=["一", "二"], confirmed=True
            )
        self.assertIn("feishu_login", res.error)
        self.assertIn("token", res.error)
        self.assertNotIn("在开放平台后台勾上", res.error)

    def test_a_board_block_without_a_token_is_an_error_not_a_silent_success(self):
        def fake(method, url, **kw):
            return {"code": 0, "data": {"children": [{"block_id": "blk1", "board": {}}]}}

        p1, p2, p3 = self._patched(fake)
        with p1, p2, p3:
            res = create_board(cfg(), url="https://x.feishu.cn/docx/doc1", confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("board.token", res.error)


class DrawIntoExistingBoardTests(unittest.TestCase):
    def test_refuses_without_explicit_confirmation(self):
        with mock.patch("core.feishu_board._http_json") as http:
            res = draw_board_flow(cfg(), "wb1", ["一"])
        self.assertFalse(res.ok)
        http.assert_not_called()

    def test_missing_whiteboard_id_is_caught_locally(self):
        with mock.patch("core.feishu_board._http_json") as http:
            res = draw_board_flow(cfg(), "  ", ["一"], confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("whiteboard_id", res.error)
        http.assert_not_called()

    def test_nodes_are_posted_to_the_given_board(self):
        seen = {}

        def fake(method, url, *, headers=None, body=None, timeout=30.0):
            seen["url"] = url
            return {"code": 0, "data": {"ids": ["a", "b", "c"]}}

        with mock.patch("core.feishu_board._http_json", side_effect=fake), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = draw_board_flow(cfg(), "wb9", ["一", "二"], confirmed=True)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.nodes_written, 3)
        self.assertIn("/whiteboards/wb9/nodes", seen["url"])

    def test_too_many_nodes_is_refused_before_the_request(self):
        with mock.patch("core.feishu_board._http_json") as http:
            res = draw_board_flow(
                cfg(), "wb1", [f"步骤{i}" for i in range(MAX_NODES)], confirmed=True
            )
        self.assertFalse(res.ok)
        self.assertIn(str(MAX_NODES), res.error)
        http.assert_not_called()


class ListBoardsTests(unittest.TestCase):
    def test_only_board_blocks_are_listed(self):
        blocks = [
            {"block_id": "b1", "block_type": 2, "text": {}},
            {"block_id": "b2", "block_type": BOARD_BLOCK_TYPE, "board": {"token": "wb1"}},
            {"block_id": "b3", "block_type": BOARD_BLOCK_TYPE, "board": {"token": "wb2"}},
        ]
        with mock.patch("core.feishu._all_blocks", return_value=blocks), mock.patch(
            "core.feishu_board._resolve_document_id", return_value=("doc1", "标题")
        ), mock.patch("core.feishu_oauth.ensure_user_access_token", return_value="u-tok"):
            boards, err = list_document_boards(cfg(), "https://x.feishu.cn/docx/doc1")
        self.assertEqual(err, "")
        self.assertEqual([b["whiteboard_id"] for b in boards], ["wb1", "wb2"])

    def test_a_non_feishu_link_is_rejected(self):
        boards, err = list_document_boards(cfg(), "https://example.com/x")
        self.assertEqual(boards, [])
        self.assertIn("链接", err)


class BoardMcpToolTests(unittest.TestCase):
    def _tool(self, name):
        import mcp_server

        return next((t for t in mcp_server.TOOLS if t["name"] == name), None)

    def test_the_three_board_tools_are_registered(self):
        for name in (
            "memory_feishu_create_board",
            "memory_feishu_board_draw",
            "memory_feishu_list_boards",
        ):
            self.assertIsNotNone(self._tool(name), name)

    def test_board_writes_require_confirmed_in_the_schema(self):
        for name in ("memory_feishu_create_board", "memory_feishu_board_draw"):
            required = self._tool(name)["inputSchema"]["required"]
            self.assertIn("confirmed", required, name)

    def test_listing_boards_is_read_only_and_needs_no_confirmation(self):
        required = self._tool("memory_feishu_list_boards")["inputSchema"]["required"]
        self.assertNotIn("confirmed", required)

    def test_unconfirmed_calls_never_reach_the_network(self):
        import mcp_server

        with mock.patch("core.feishu._http_json") as http:
            out = mcp_server._call_feishu_tool(
                mock.MagicMock(), "memory_feishu_create_board", {"title": "x"}
            )
        self.assertTrue(out["isError"])
        http.assert_not_called()

    def test_a_created_board_is_written_to_memory(self):
        import mcp_server
        from core.feishu_board import FeishuBoardResult

        sb = mock.MagicMock()
        sb.remember_feishu_write.return_value = "已写入长时记忆 [x]"
        res = FeishuBoardResult(
            ok=True,
            whiteboard_id="wb1",
            block_id="blk1",
            document_id="doc1",
            url="https://x.feishu.cn/docx/doc1",
            title="流程",
            nodes_written=3,
        )
        with mock.patch("core.feishu_board.create_board", return_value=res):
            out = mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_create_board",
                {"title": "流程", "steps": ["一", "二"], "confirmed": True},
            )
        self.assertFalse(out["isError"])
        kw = sb.remember_feishu_write.call_args.kwargs
        self.assertEqual(kw["action"], "board")
        self.assertEqual(kw["blocks_written"], 3)

    def test_drawing_into_an_existing_board_writes_no_orphan_memory(self):
        import mcp_server
        from core.feishu_board import FeishuBoardResult

        sb = mock.MagicMock()
        with mock.patch(
            "core.feishu_board.draw_board_flow",
            return_value=FeishuBoardResult(ok=True, whiteboard_id="wb1", nodes_written=3),
        ):
            mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_board_draw",
                {"whiteboard_id": "wb1", "steps": ["一", "二"], "confirmed": True},
            )
        sb.remember_feishu_write.assert_not_called()


class BoardScopeTests(unittest.TestCase):
    def test_the_node_create_scope_reaches_stale_configs(self):
        """只写进 FeishuConfig 默认值不够：存过配置的机器会用自己那份旧 scope 覆盖掉。"""
        from core.feishu_oauth import _merged_scopes

        merged = _merged_scopes(
            FeishuConfig(app_id="cli_x", oauth_scope="offline_access wiki:node:read")
        ).split()
        self.assertIn(NODE_CREATE_SCOPE, merged)


if __name__ == "__main__":
    unittest.main()
