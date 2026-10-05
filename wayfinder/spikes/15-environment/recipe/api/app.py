"""Tiny API for the spike: GET /hello returns the seeded greeting from redis.

Logs one line per request to stdout, which is what an API Proof would quote.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis

r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            try:
                r.ping()
                self._send(200, {"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._send(503, {"ok": False, "error": str(exc)})
        elif self.path == "/hello":
            self._send(200, {"greeting": r.get("greeting") or "(not seeded)"})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # one line per request
        print(f"api {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
