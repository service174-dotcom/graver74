#!/usr/bin/env python3
"""reel_sidecar — постоянный HTTP-вход к reel_fetch.

Зачем: MCP-инструмент addons_call существует только если модуль попал в реестр
диспетчера аддонов. Сайдкар даёт гарантированный REST-вход (/reel) и не зависит
от правок bridge_server.py/mcp_server.py. Слушает только loopback, наружу — через Caddy.

Запуск: reel_sidecar.py --host 127.0.0.1 --port 8791
Авторизация: X-API-Key = BRIDGE_API_KEY из /opt/social_connector/.env (или MCP_API_KEY).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reel_fetch  # noqa: E402,WPS433

ENV_FILE = Path(os.environ.get("REEL_ENV_FILE", "/opt/social_connector/.env"))


def _load_api_key() -> str:
    key = os.environ.get("MCP_API_KEY") or os.environ.get("BRIDGE_API_KEY") or ""
    if key:
        return key
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() in ("MCP_API_KEY", "BRIDGE_API_KEY") and value.strip():
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


API_KEY = _load_api_key()


class Handler(BaseHTTPRequestHandler):
    server_version = "reel-sidecar/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not API_KEY:
            return False
        given = (self.headers.get("X-API-Key") or self.headers.get("x-api-key")
                 or self.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        return given == API_KEY

    def _params(self, query: dict) -> dict:
        params: dict = {}
        for name, values in query.items():
            if name in ("action", "key"):
                continue
            if name == "params":
                try:
                    parsed = json.loads(values[-1])
                    if isinstance(parsed, dict):
                        params.update(parsed)
                except json.JSONDecodeError:
                    pass
                continue
            params[name] = values[-1]
        for flag in ("audio_only", "transcribe", "keep_video"):
            if flag in params and isinstance(params[flag], str):
                params[flag] = params[flag].lower() in ("1", "true", "yes", "on")
        for num in ("max_seconds", "limit", "days"):
            if num in params and isinstance(params[num], str) and params[num].isdigit():
                params[num] = int(params[num])
        return params

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/reel/health"):
            return self._send(200, {"ok": True, "module": "reel_sidecar", "auth_required": bool(API_KEY),
                                    "reel": reel_fetch.health()})
        if parsed.path not in ("/reel", "/reel/", "/"):
            return self._send(404, {"ok": False, "error": "not found"})
        if not self._authed():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        query = parse_qs(parsed.query, keep_blank_values=True)
        action = (query.get("action") or ["fetch"])[0]
        try:
            return self._send(200, reel_fetch.run_action(action, **self._params(query)))
        except reel_fetch.ReelError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/reel", "/reel/"):
            return self._send(404, {"ok": False, "error": "not found"})
        if not self._authed():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"ok": False, "error": "body must be JSON"})
        action = body.pop("action", "fetch")
        params = body.pop("params", body) or {}
        try:
            return self._send(200, reel_fetch.run_action(action, **params))
        except reel_fetch.ReelError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    reel_fetch._ensure_media_dir()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"reel_sidecar on {args.host}:{args.port} auth={'on' if API_KEY else 'OFF'}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
