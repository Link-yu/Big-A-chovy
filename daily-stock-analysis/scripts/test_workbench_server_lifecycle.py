"""HTTP server ownership and shutdown tests using loopback-only temporary ports."""

from __future__ import annotations

import argparse
import contextlib
import io
import socket
import threading
import time
import unittest
from unittest.mock import patch

import realtime_dashboard as dash
import web_workbench as workbench


class WorkbenchServerLifecycleTests(unittest.TestCase):
    def _assert_port_released(self, port: int) -> None:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    def test_scheduler_shutdown_exits_serving_loop_closes_socket_and_detaches(self) -> None:
        started_shutdown = threading.Event()
        scheduler_saw_attached_server = []
        shutdown_thread = None
        captured = {}
        base_server = workbench.ThreadingHTTPServer
        stop_was_set = dash.scheduler._stop_event.is_set()
        dash.scheduler._stop_event.clear()

        def make_server(address, handler):
            server = base_server(address, handler)
            captured["port"] = server.server_address[1]
            return server

        def fake_scheduler_start():
            def request_shutdown():
                deadline = time.monotonic() + 2
                while dash._server is None and time.monotonic() < deadline:
                    time.sleep(0.005)
                scheduler_saw_attached_server.append(dash._server is not None)
                dash._shutdown_server()
                started_shutdown.set()

            nonlocal shutdown_thread
            shutdown_thread = threading.Thread(target=request_shutdown, daemon=True)
            shutdown_thread.start()

        args = argparse.Namespace(port=0, host="127.0.0.1", no_browser=True, no_dashboard_refresh=False)
        try:
            with (
                patch.object(workbench, "_ensure_utf8_stdio"),
                patch("argparse.ArgumentParser.parse_args", return_value=args),
                patch.object(workbench, "ThreadingHTTPServer", side_effect=make_server),
                patch.object(dash.scheduler, "start", side_effect=fake_scheduler_start),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(workbench.main(), 0)
            self.assertTrue(started_shutdown.wait(timeout=1))
            if shutdown_thread is not None:
                shutdown_thread.join(timeout=1)
            self.assertEqual(scheduler_saw_attached_server, [True])
            self.assertIsNone(dash._server)
            self._assert_port_released(captured["port"])
        finally:
            if stop_was_set:
                dash.scheduler._stop_event.set()
            else:
                dash.scheduler._stop_event.clear()

    def test_keyboard_interrupt_closes_socket_without_same_thread_shutdown(self) -> None:
        base_server = workbench.ThreadingHTTPServer

        class InterruptingServer(base_server):
            def serve_forever(self, *args, **kwargs):
                raise KeyboardInterrupt

        args = argparse.Namespace(port=0, host="127.0.0.1", no_browser=True, no_dashboard_refresh=True)
        stop_was_set = dash.scheduler._stop_event.is_set()
        dash.scheduler._stop_event.clear()
        captured = {}

        def make_server(address, handler):
            server = InterruptingServer(address, handler)
            captured["port"] = server.server_address[1]
            return server

        try:
            with (
                patch.object(workbench, "_ensure_utf8_stdio"),
                patch("argparse.ArgumentParser.parse_args", return_value=args),
                patch.object(workbench, "ThreadingHTTPServer", side_effect=make_server),
                patch.object(dash.scheduler, "_archive_markdown"),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(workbench.main(), 0)
            self.assertIsNone(dash._server)
            self._assert_port_released(captured["port"])
        finally:
            if stop_was_set:
                dash.scheduler._stop_event.set()
            else:
                dash.scheduler._stop_event.clear()


if __name__ == "__main__":
    unittest.main()
