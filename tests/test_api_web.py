"""app_web 的 HTTP 行为：错误响应的 CORS 头、旧后端识别。"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_web  # noqa: E402


class ApiErrorResponseTests(unittest.TestCase):
    """
    错误响应必须带 CORS 头。

    默认错误页不带 Access-Control-Allow-Origin，跨域调用时浏览器会把响应整个拦掉，
    前端只剩一句 TypeError: Load failed，看不出是 404 还是后端挂了。
    """

    ORIGIN = "http://localhost:5173"

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app_web.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Origin": self.ORIGIN},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Origin": self.ORIGIN, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")

    def test_unknown_api_path_404_carries_cors(self):
        code, headers, body = self._get("/api/definitely-not-here")
        self.assertEqual(code, 404)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.ORIGIN)
        self.assertFalse(body["ok"])

    def test_unknown_api_path_explains_stale_backend(self):
        """新接口 404 最常见的原因就是旧后端还在跑，错误里要说出来。"""
        _, _, body = self._get("/api/definitely-not-here")
        self.assertIn("旧版后端", body.get("hint", ""))
        self.assertEqual(body.get("build"), app_web.UI_BUILD)

    def test_hint_folded_into_error_field(self):
        """前端（parseJson）只展示 error，提示不折进去等于没说。"""
        _, _, body = self._get("/api/definitely-not-here")
        self.assertIn("旧版后端", body["error"])
        self.assertIn(app_web.UI_BUILD, body["error"])

    def test_error_body_is_json_not_html(self):
        _, headers, _ = self._get("/api/definitely-not-here")
        self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_health_advertises_cursor_hooks(self):
        code, headers, body = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), self.ORIGIN)
        self.assertIn("cursor_hooks", body["features"])

    def test_hooks_status_endpoint_is_served(self):
        code, _, body = self._get("/api/cursor_hooks/status")
        self.assertEqual(code, 200)
        self.assertIn("installed", body)

    def test_prepare_endpoint_returns_context_pack(self):
        """hook 预取用的接口：给 query 回软召回文本。"""
        calls = []

        def _pack(query, top_k=5):
            calls.append((query, top_k))
            return {
                "references": [{"id": "x"}],
                "context_pack": "【参考】...",
                "ref_threshold": 0.45,
            }

        stub = SimpleNamespace(sandbox=SimpleNamespace(build_reference_pack=_pack))
        old = getattr(app_web, "STATE", None)
        app_web.STATE = stub
        try:
            code, _, body = self._post("/api/prepare", {"query": "zIndex 治理"})
        finally:
            app_web.STATE = old
        self.assertEqual(code, 200)
        self.assertEqual(body["context_pack"], "【参考】...")
        self.assertEqual(body["reference_count"], 1)
        self.assertEqual(calls, [("zIndex 治理", 5)])

    def test_prepare_endpoint_rejects_empty_query(self):
        code, _, body = self._post("/api/prepare", {"query": "  "})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_revision_endpoint_returns_stamp(self):
        """轮询接口：只回变更标记，不回记忆内容。"""
        stub = SimpleNamespace(
            sandbox=SimpleNamespace(
                long_term=SimpleNamespace(revision=lambda: "42:7"),
                knowledge=SimpleNamespace(revision=lambda: "9:1"),
            )
        )
        old = getattr(app_web, "STATE", None)
        app_web.STATE = stub
        try:
            code, _, body = self._get("/api/long_term_revision")
        finally:
            app_web.STATE = old
        self.assertEqual(code, 200)
        self.assertEqual(body["revision"], "42:7")
        self.assertNotIn("declarative", body)

    def test_revision_endpoint_also_watches_knowledge(self):
        """后台抓完文档得让界面自己刷出来，所以两个标记走同一个轮询。"""
        stub = SimpleNamespace(
            sandbox=SimpleNamespace(
                long_term=SimpleNamespace(revision=lambda: "42:7"),
                knowledge=SimpleNamespace(revision=lambda: "9:1"),
            )
        )
        old = getattr(app_web, "STATE", None)
        app_web.STATE = stub
        try:
            _, _, body = self._get("/api/long_term_revision")
        finally:
            app_web.STATE = old
        self.assertEqual(body["knowledge_revision"], "9:1")

    # ---------- 知识库 ----------
    def _knowledge_stub(self, **overrides):
        doc = {"id": "d1", "title": "飞书接入手册", "url": "https://x/docx/T1"}
        kb = SimpleNamespace(
            list_docs=lambda: [doc],
            stats=lambda: {"doc_count": 1, "chunk_count": 3, "failed_count": 0},
            read_doc=lambda i: {**doc, "chunks": []} if i == "d1" else None,
            delete=lambda i: i == "d1",
        )
        sandbox = SimpleNamespace(
            knowledge=kb,
            add_knowledge=lambda url, **kw: {"ok": True, "doc": doc, "skipped": False},
            refresh_knowledge=lambda i: {"ok": True, "doc": doc},
        )
        for k, v in overrides.items():
            setattr(sandbox, k, v)
        return SimpleNamespace(sandbox=sandbox, status_line=lambda: "ok")

    def _with_knowledge(self, path, payload, **overrides):
        old = getattr(app_web, "STATE", None)
        app_web.STATE = self._knowledge_stub(**overrides)
        try:
            return self._post(path, payload)
        finally:
            app_web.STATE = old

    def test_knowledge_list_returns_docs_and_stats(self):
        code, _, body = self._with_knowledge("/api/knowledge/list", {})
        self.assertEqual(code, 200)
        self.assertEqual(body["docs"][0]["title"], "飞书接入手册")
        self.assertEqual(body["stats"]["doc_count"], 1)

    def test_knowledge_add_requires_a_url(self):
        code, _, body = self._with_knowledge("/api/knowledge/add", {"url": "  "})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_knowledge_add_returns_the_stored_doc(self):
        code, _, body = self._with_knowledge(
            "/api/knowledge/add", {"url": "https://x/docx/T1"}
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["doc"]["id"], "d1")

    def test_knowledge_add_surfaces_the_failure_reason(self):
        """抓不动要说清为什么，不然用户只知道「没成功」。"""
        code, _, body = self._with_knowledge(
            "/api/knowledge/add",
            {"url": "https://x/docx/T1"},
            add_knowledge=lambda url, **kw: {"ok": False, "error": "没有权限"},
        )
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "没有权限")

    def test_knowledge_get_returns_chunks(self):
        code, _, body = self._with_knowledge("/api/knowledge/get", {"id": "d1"})
        self.assertEqual(code, 200)
        self.assertIn("chunks", body["doc"])

    def test_knowledge_get_unknown_id_is_404(self):
        code, _, body = self._with_knowledge("/api/knowledge/get", {"id": "nope"})
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_knowledge_refresh_refetches(self):
        code, _, body = self._with_knowledge("/api/knowledge/refresh", {"id": "d1"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_knowledge_delete_reports_missing(self):
        code, _, body = self._with_knowledge("/api/knowledge/delete", {"id": "nope"})
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_knowledge_delete_ok(self):
        code, _, body = self._with_knowledge("/api/knowledge/delete", {"id": "d1"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

    def test_health_advertises_knowledge_base(self):
        """Tauri 靠特性名认旧后端，不报的话新 UI 会连上没有这些路由的旧进程。"""
        _, _, body = self._get("/api/health")
        self.assertIn("knowledge_base", body["features"])


class MissingFeatureTests(unittest.TestCase):
    """只比对单个特性认不出「加了新接口的旧进程」：它照样自称健康。"""

    def test_backend_without_new_feature_is_stale(self):
        # 从 UI_FEATURES 派生，别写死列表：每加一个特性都要来改测试就没人愿意加了
        stale = {"features": [f for f in app_web.UI_FEATURES if f != "cursor_hooks"]}
        self.assertEqual(app_web.missing_features(stale), ["cursor_hooks"])

    def test_current_version_lacks_nothing(self):
        current = {"features": list(app_web.UI_FEATURES)}
        self.assertEqual(app_web.missing_features(current), [])

    def test_no_features_reported_is_stale(self):
        self.assertEqual(
            app_web.missing_features({}), list(app_web.UI_FEATURES)
        )


class CodeStampTests(unittest.TestCase):
    """特性名不变、只改 core/ 的旧进程也要能认出来。"""

    # 与 Rust 侧共享的契约值，两边算法一旦分叉就会有一侧挂掉。
    # 见 desktop/src-tauri/src/api_server.rs::tests::SAMPLE_STAMP
    SAMPLE_STAMP = "ca0f047bc734"

    def _sample_tree(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="stamp_"))
        (root / "core").mkdir()
        (root / "app_web.py").write_bytes(b"print(1)\n")
        (root / "core" / "a.py").write_bytes(b"A\n")
        (root / "core" / "b.py").write_bytes(b"B\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def test_matches_rust_contract_sample(self):
        self.assertEqual(
            app_web.compute_code_stamp(self._sample_tree()), self.SAMPLE_STAMP
        )

    def test_pycache_does_not_affect_stamp(self):
        # 打包时 rsync 排除 __pycache__，开发目录里却有；两处指纹必须相同
        root = self._sample_tree()
        (root / "core" / "__pycache__").mkdir()
        (root / "core" / "__pycache__" / "a.cpython-39.pyc").write_bytes(b"junk")
        self.assertEqual(app_web.compute_code_stamp(root), self.SAMPLE_STAMP)

    def test_editing_a_core_file_changes_stamp(self):
        root = self._sample_tree()
        (root / "core" / "b.py").write_bytes(b"B2\n")
        self.assertNotEqual(app_web.compute_code_stamp(root), self.SAMPLE_STAMP)

    def test_incomplete_tree_yields_no_stamp(self):
        root = self._sample_tree()
        (root / "app_web.py").unlink()
        self.assertEqual(app_web.compute_code_stamp(root), "")

    def test_stamp_is_stable_across_calls(self):
        root = self._sample_tree()
        self.assertEqual(
            app_web.compute_code_stamp(root), app_web.compute_code_stamp(root)
        )

    def test_real_tree_has_a_stamp(self):
        self.assertRegex(app_web.CODE_STAMP, r"^[0-9a-f]{12}$")

    def test_stale_only_when_both_stamps_known(self):
        current = app_web.CODE_STAMP
        self.assertFalse(app_web.has_stale_code({}), "旧后端没这个字段时不下判断")
        self.assertFalse(app_web.has_stale_code({"code_stamp": current}))
        self.assertTrue(app_web.has_stale_code({"code_stamp": "0" * 12}))

    def test_health_reports_code_stamp(self):
        self.assertEqual(_health_payload().get("code_stamp"), app_web.CODE_STAMP)


def _health_payload():
    """起一次真服务读 /api/health，确认 code_stamp 真的发出去了。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app_web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=5
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    unittest.main()
