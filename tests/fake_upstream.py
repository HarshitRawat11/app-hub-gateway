"""A deliberately misbehaving stand-in for links-service.

Development fixture, not a test. The 502 and 504 paths in gateway cannot be
exercised against a healthy links-service -- you need an upstream that returns
an error, hangs, or answers with a non-JSON body. This provides all three.

    python3 fake_upstream.py <port> <mode>

Modes:
    ok        200 with an empty JSON array
    404       404 with a JSON body      -> gateway should answer 502
    html500   500 with an HTML body     -> gateway should answer 502, and the
                                           status check must run BEFORE
                                           response.json(), or the parser
                                           raises and you get a 500 by accident
    slow      sleeps 30s then 200       -> gateway should answer 504 at ~3s

Point gateway at it with LINKS_SERVICE_URL, e.g.

    LINKS_SERVICE_URL=http://localhost:8002 uv run uvicorn app.main:app --port 8001
"""

import http.server
import sys
import time

PORT = int(sys.argv[1])
MODE = sys.argv[2]


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if MODE == "ok":
            self._send(200, b"[]", "application/json")
        elif MODE == "404":
            self._send(404, b'{"detail":"Not Found"}', "application/json")
        elif MODE == "html500":
            self._send(500, b"<html><body>500</body></html>", "text/html")
        elif MODE == "slow":
            time.sleep(30)
            self._send(200, b"[]", "application/json")
        else:
            raise SystemExit(f"unknown mode: {MODE}")

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
