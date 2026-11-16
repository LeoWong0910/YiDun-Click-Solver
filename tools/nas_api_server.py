#!/usr/bin/env python3
"""Minimal HTTP API for running the YiDun click solver on Synology NAS.

Endpoints:
  GET  /health          -> liveness
  GET  /                 -> short usage
  POST /solve            -> run auto solve; JSON body may override CLI flags
  GET  /solve            -> same, query-string overrides for quick tests

Auth (optional): when env YIDUN_API_TOKEN is set, require
  Authorization: Bearer <token>  or  X-API-Token: <token>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SOLVER = ROOT / "tools" / "yidun_click_solver.py"
HOST = os.environ.get("YIDUN_HOST", "0.0.0.0")
PORT = int(os.environ.get("YIDUN_PORT", "8765"))
API_TOKEN = os.environ.get("YIDUN_API_TOKEN", "").strip()
DEFAULT_MAX_ATTEMPTS = int(os.environ.get("YIDUN_MAX_ATTEMPTS", "10"))
DEFAULT_SOLVE_TIMEOUT = float(os.environ.get("YIDUN_SOLVE_TIMEOUT", "120"))

# Serialize solves so OpenCV/network work does not thrash a low-power NAS CPU.
_SOLVE_LOCK = threading.Lock()

# Body/query keys mapped to CLI flags (string values unless noted).
_FLAG_MAP = {
    "captcha_id": "--captcha-id",
    "dt": "--dt",
    "zone_id": "--zone-id",
    "ir_token": "--ir-token",
    "fp": "--fp",
    "token": "--token",
    "points": "--points",
    "max_attempts": "--max-attempts",
    "auto_alternatives": "--auto-alternatives",
    "timeout": "--timeout",
    "page_referer": "--page-referer",
    "query_referer": "--query-referer",
    "get_only": None,  # boolean flag
    "output_detail": "--output-detail",
}


def _json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return status, body


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    if not API_TOKEN:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].strip() == API_TOKEN:
        return True
    return handler.headers.get("X-API-Token", "").strip() == API_TOKEN


def _merge_params(query: dict[str, list[str]], body: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, values in query.items():
        if not values:
            continue
        merged[key] = values[-1]
    if body:
        for key, value in body.items():
            merged[key] = value
    return merged


def _build_argv(params: dict[str, Any]) -> list[str]:
    argv: list[str] = [sys.executable, str(SOLVER)]
    max_attempts = params.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    argv.extend(["--max-attempts", str(max_attempts)])

    if params.get("get_only") in (True, "1", "true", "True", "yes"):
        argv.append("--get-only")

    for key, flag in _FLAG_MAP.items():
        if key in {"max_attempts", "get_only"} or flag is None:
            continue
        if key not in params or params[key] in (None, ""):
            continue
        argv.extend([flag, str(params[key])])

    # Keep only useful bypass fields unless the caller asks for debug.
    detail = str(params.get("output_detail", "useful"))
    if detail not in {"useful", "debug"}:
        detail = "useful"
    argv.extend(["--output-detail", detail])
    return argv


def _run_solver(params: dict[str, Any]) -> dict[str, Any]:
    argv = _build_argv(params)
    timeout = float(params.get("solve_timeout", DEFAULT_SOLVE_TIMEOUT))
    env = os.environ.copy()
    # Allow callers to pass through common YIDUN_* overrides via JSON body.
    for env_key in ("YIDUN_FP", "YIDUN_IR_TOKEN", "YIDUN_DT", "YIDUN_ZONE_ID"):
        short = env_key.removeprefix("YIDUN_").lower()
        if short in params and params[short] not in (None, ""):
            env[env_key] = str(params[short])
        body_env = params.get("env")
        if isinstance(body_env, dict) and env_key in body_env:
            env[env_key] = str(body_env[env_key])

    with _SOLVE_LOCK:
        proc = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parsed: Any = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = {"raw_stdout": stdout}

    ok = bool(isinstance(parsed, dict) and parsed.get("ok")) or proc.returncode == 0
    return {
        "ok": ok,
        "exit_code": proc.returncode,
        "result": parsed,
        "stderr": stderr[-4000:] if stderr else "",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "YiDunNasAPI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/healthz"):
            status, body = _json_bytes({"ok": True, "service": "yidun-click-solver"})
            self._send(status, body)
            return
        if parsed.path in ("/", "/usage"):
            status, body = _json_bytes(
                {
                    "service": "yidun-click-solver",
                    "endpoints": {
                        "GET /health": "liveness",
                        "POST /solve": "auto solve; optional JSON overrides",
                        "GET /solve": "same via query string",
                    },
                    "auth_required": bool(API_TOKEN),
                }
            )
            self._send(status, body)
            return
        if parsed.path == "/solve":
            self._handle_solve(parse_qs(parsed.query), None)
            return
        status, body = _json_bytes({"ok": False, "error": "not found"}, 404)
        self._send(status, body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/solve":
            status, body = _json_bytes({"ok": False, "error": "not found"}, 404)
            self._send(status, body)
            return
        try:
            body_obj = self._read_json_body()
        except ValueError as exc:
            status, body = _json_bytes({"ok": False, "error": str(exc)}, 400)
            self._send(status, body)
            return
        self._handle_solve(parse_qs(parsed.query), body_obj)

    def _handle_solve(self, query: dict[str, list[str]], body: dict[str, Any] | None) -> None:
        if not _authorized(self):
            status, payload = _json_bytes({"ok": False, "error": "unauthorized"}, 401)
            self._send(status, payload)
            return
        params = _merge_params(query, body)
        try:
            result = _run_solver(params)
            http_status = 200 if result.get("ok") else 422
            status, payload = _json_bytes(result, http_status)
            self._send(status, payload)
        except subprocess.TimeoutExpired:
            status, payload = _json_bytes(
                {"ok": False, "error": "solve timed out"},
                504,
            )
            self._send(status, payload)
        except Exception as exc:  # noqa: BLE001 — surface unexpected failures to caller
            status, payload = _json_bytes(
                {
                    "ok": False,
                    "error": str(exc),
                    "trace": traceback.format_exc()[-2000:],
                },
                500,
            )
            self._send(status, payload)


def main() -> int:
    if not SOLVER.is_file():
        print(f"solver not found: {SOLVER}", file=sys.stderr)
        return 1
    # Ensure writable runtime dirs exist inside the container volume mounts.
    (ROOT / "captures").mkdir(parents=True, exist_ok=True)
    (ROOT / "results").mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    auth_state = "enabled" if API_TOKEN else "disabled"
    print(f"YiDun NAS API listening on http://{HOST}:{PORT} (auth={auth_state})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
