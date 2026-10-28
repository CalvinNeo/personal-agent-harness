#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
import os
from urllib.parse import urlsplit

UPSTREAM = os.environ.get("HARNESS_UPSTREAM_ORIGIN", "").rstrip("/")
TOKEN_FILE = "/var/lib/codexproxy/upstream-token"
LISTEN = ("127.0.0.1", 6186)
MAX_BODY = 2 * 1024 * 1024

class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.forward()
    def do_POST(self): self.forward()
    def do_OPTIONS(self): self.send_error(405)
    def log_message(self, fmt, *args): pass
    def forward(self):
        if self.path.split("?", 1)[0] not in {"/v1/responses", "/v1/models"}:
            self.send_error(404); return
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as token_handle:
                token = token_handle.read().strip()
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > MAX_BODY:
                self.send_error(413); return
            data = self.rfile.read(n) if n else None
            req = Request(UPSTREAM + self.path, data=data, method=self.command, headers={
                "Authorization": "Bearer " + token,
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Accept": self.headers.get("Accept", "application/json"),
            })
            with urlopen(req, timeout=900) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
        except Exception as exc:
            self.send_error(502, "upstream unavailable")

if __name__ == "__main__":
    parsed = urlsplit(UPSTREAM)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise SystemExit("Set HARNESS_UPSTREAM_ORIGIN to an HTTPS origin without credentials or a path")
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
