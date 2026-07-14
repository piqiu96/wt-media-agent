"""Local Agent control API scaffold."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalApiServer:
    """Exposes the M0 local process health surface."""

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": "wt-media-agent", "mode": "m0"}


def make_handler(api: LocalApiServer) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "WTMediaLocalAgentM0/0.1"

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._write_json(200, api.health())
                return
            self._write_json(404, {"error": "not_found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_json(self, status: int, payload: dict[str, str]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return HealthHandler


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    api = LocalApiServer()
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    print(f"wt-media-agent local health listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("wt-media-agent local health stopped", flush=True)
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
