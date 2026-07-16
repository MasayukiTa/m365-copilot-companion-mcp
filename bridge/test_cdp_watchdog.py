from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from bridge import copilot_bridge as bridge


class _CdpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json/version":
            body = b'{"Browser":"Edge"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def test_cdp_watchdog_health_probe_distinguishes_live_and_dead_ports():
    server = HTTPServer(("127.0.0.1", 0), _CdpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        assert bridge._cdp_healthy("http://127.0.0.1:%d" % port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert not bridge._cdp_healthy("http://127.0.0.1:%d" % port, timeout=0.1)


def test_main_starts_watchdog_before_serving_forever():
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert "_start_cdp_watchdog(cdp)" in source
    assert "os._exit(70)" in source
    assert 'tool_probe.record_probe(False, "starting"' in source
