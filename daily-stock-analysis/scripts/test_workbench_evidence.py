"""HTTP boundary checks for the new on-demand evidence routes."""

from __future__ import annotations

import http.client
import json
import threading
import unittest

from test_web_workbench import _load_workbench


class WorkbenchEvidenceRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workbench = _load_workbench()

    def test_context_topic_is_allowlisted_before_source_call(self):
        calls = []
        original = self.workbench._tool_context
        self.workbench._tool_context = lambda *args, **kwargs: calls.append((args, kwargs)) or {"status": "ok", "data": {"safe": True}}
        server = self.workbench.ThreadedHTTPServer if hasattr(self.workbench, "ThreadedHTTPServer") else None
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.workbench.WorkbenchHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/api/wb/context?code=600519&topic=arbitrary_url")
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            payload = json.loads(response.read())
            self.assertEqual(payload["status"], "unsupported")
            self.assertEqual(calls, [])
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/api/wb/context?code=600519&topic=themes")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["status"], "ok")
            self.assertEqual(calls[0][0][0], "600519")
            conn.close()
        finally:
            self.workbench._tool_context = original
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_static_evidence_panel_escapes_untrusted_summary(self):
        html = (self.workbench.WORKBENCH_STATIC / "index.html").read_text(encoding="utf-8")
        js = (self.workbench.WORKBENCH_STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("证据核验舱", html)
        self.assertIn("aria-live=\"polite\"", html)
        self.assertIn("evidenceLinks", js)
        self.assertIn("function evidenceDetail", js)
        self.assertIn("不能据此断言", js)
        self.assertIn("five_book", js)
        self.assertIn("esc(JSON.stringify(detail", js)
        self.assertNotIn("innerHTML = payload", js)


if __name__ == "__main__":
    unittest.main()
