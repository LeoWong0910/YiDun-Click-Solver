#!/usr/bin/env python3
"""HTTP API for YiDun click solver on Synology NAS.

Endpoints:
  GET  /health              liveness
  GET  /                    short usage
  POST /solve               full auto fetch + recognize + check
  GET  /solve               same via query string
  POST /recognize           local CV recognition only (image_path / image_base64)
  POST /notify/test         send a test notification with current env config

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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.notify import NotifyConfig, build_solve_message, notify_all  # noqa: E402
from src.recognize import recognize_image_base64, recognize_image_file  # noqa: E402

SOLVER = ROOT / "tools" / "yidun_click_solver.py"
HOST = os.environ.get("YIDUN_HOST", "0.0.0.0")
PORT = int(os.environ.get("YIDUN_PORT", "8765"))
API_TOKEN = os.environ.get("YIDUN_API_TOKEN", "").strip()
DEFAULT_MAX_ATTEMPTS = int(os.environ.get("YIDUN_MAX_ATTEMPTS", "10"))
DEFAULT_SOLVE_TIMEOUT = float(os.environ.get("YIDUN_SOLVE_TIMEOUT", "120"))
NOTIFY_CFG = NotifyConfig.from_env()

# Serialize solves so OpenCV/network work does not thrash a low-power NAS CPU.
_SOLVE_LOCK = threading.Lock()

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
    "get_only": None,
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _build_argv(params: dict[str, Any]) -> list[str]:
    argv: list[str] = [sys.executable, str(SOLVER)]
    max_attempts = params.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    argv.extend(["--max-attempts", str(max_attempts)])

    if _truthy(params.get("get_only")):
        argv.append("--get-only")
    else:
        # Default path keeps original CLI auto recognition enabled.
        argv.append("--auto")

    for key, flag in _FLAG_MAP.items():
        if key in {"max_attempts", "get_only"} or flag is None:
            continue
        if key not in params or params[key] in (None, ""):
            continue
        argv.extend([flag, str(params[key])])

    detail = str(params.get("output_detail", "useful"))
    if detail not in {"useful", "debug"}:
        detail = "useful"
    argv.extend(["--output-detail", detail])
    return argv


def _run_solver(params: dict[str, Any]) -> dict[str, Any]:
    argv = _build_argv(params)
    timeout = float(params.get("solve_timeout", DEFAULT_SOLVE_TIMEOUT))
    env = os.environ.copy()
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

    if isinstance(parsed, dict) and "ok" in parsed:
        ok = bool(parsed.get("ok"))
    else:
        ok = proc.returncode == 0

    return {
        "ok": ok,
        "exit_code": proc.returncode,
        "result": parsed,
        "stderr": stderr[-4000:] if stderr else "",
    }


def _maybe_notify_solve(result: dict[str, Any], params: dict[str, Any]) -> dict[str, Any] | None:
    # Per-request override: notify=false skips; notify=true forces attempt.
    if "notify" in params and not _truthy(params.get("notify")):
        return {"notified": False, "skipped": "request_disabled"}
    cfg = NOTIFY_CFG
    if "notify" in params and _truthy(params.get("notify")) and not cfg.enabled:
        # Request asked to notify but env has no channels.
        return {"notified": False, "skipped": "no_channels"}
    if not cfg.enabled and "notify" not in params:
        return None

    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    title, body = build_solve_message(ok=bool(result.get("ok")), result=payload)
    return notify_all(
        cfg,
        ok=bool(result.get("ok")),
        title=title,
        body=body,
        extra={"exit_code": result.get("exit_code"), "ok": result.get("ok")},
    )


def _run_recognize(params: dict[str, Any]) -> dict[str, Any]:
    image_path = params.get("image_path") or params.get("path")
    image_b64 = params.get("image_base64") or params.get("image")
    debug_dir = params.get("debug_dir") or str(ROOT / "results")

    with _SOLVE_LOCK:
        if image_path:
            path = Path(str(image_path))
            if not path.is_file():
                # Allow paths relative to captures/ for NAS convenience.
                alt = ROOT / "captures" / path.name
                if alt.is_file():
                    path = alt
                else:
                    raise FileNotFoundError(f"image not found: {image_path}")
            out = recognize_image_file(path, debug_image_path=Path(debug_dir) / f"rec_{path.stem}.png")
        elif image_b64:
            out = recognize_image_base64(str(image_b64), debug_dir=debug_dir)
        else:
            raise ValueError("provide image_path or image_base64")
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "YiDunNasAPI/1.1"

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
            status, body = _json_bytes(
                {
                    "ok": True,
                    "service": "yidun-click-solver",
                    "features": ["solve", "recognize", "notify"],
                    "notify_enabled": NOTIFY_CFG.enabled,
                }
            )
            self._send(status, body)
            return
        if parsed.path in ("/", "/usage"):
            status, body = _json_bytes(
                {
                    "service": "yidun-click-solver",
                    "endpoints": {
                        "GET /health": "liveness",
                        "POST /solve": "auto fetch + recognize + check",
                        "GET /solve": "same via query string",
                        "POST /recognize": "local CV only (image_path|image_base64)",
                        "POST /notify/test": "send a test push/email",
                    },
                    "auth_required": bool(API_TOKEN),
                    "notify_enabled": NOTIFY_CFG.enabled,
                    "cli": "python tools/yidun_click_solver.py  # original CLI still works in-container",
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
        try:
            body_obj = self._read_json_body()
        except ValueError as exc:
            status, body = _json_bytes({"ok": False, "error": str(exc)}, 400)
            self._send(status, body)
            return

        if parsed.path == "/solve":
            self._handle_solve(parse_qs(parsed.query), body_obj)
            return
        if parsed.path == "/recognize":
            self._handle_recognize(parse_qs(parsed.query), body_obj)
            return
        if parsed.path in ("/notify/test", "/notify/test/"):
            self._handle_notify_test(body_obj)
            return

        status, body = _json_bytes({"ok": False, "error": "not found"}, 404)
        self._send(status, body)

    def _handle_solve(self, query: dict[str, list[str]], body: dict[str, Any] | None) -> None:
        if not _authorized(self):
            status, payload = _json_bytes({"ok": False, "error": "unauthorized"}, 401)
            self._send(status, payload)
            return
        params = _merge_params(query, body)
        try:
            result = _run_solver(params)
            notify_report = _maybe_notify_solve(result, params)
            if notify_report is not None:
                result["notify"] = notify_report
            http_status = 200 if result.get("ok") else 422
            status, payload = _json_bytes(result, http_status)
            self._send(status, payload)
        except subprocess.TimeoutExpired:
            status, payload = _json_bytes({"ok": False, "error": "solve timed out"}, 504)
            self._send(status, payload)
        except Exception as exc:  # noqa: BLE001
            status, payload = _json_bytes(
                {"ok": False, "error": str(exc), "trace": traceback.format_exc()[-2000:]},
                500,
            )
            self._send(status, payload)

    def _handle_recognize(self, query: dict[str, list[str]], body: dict[str, Any] | None) -> None:
        if not _authorized(self):
            status, payload = _json_bytes({"ok": False, "error": "unauthorized"}, 401)
            self._send(status, payload)
            return
        params = _merge_params(query, body)
        try:
            out = _run_recognize(params)
            status, payload = _json_bytes(out, 200)
            self._send(status, payload)
        except (FileNotFoundError, ValueError) as exc:
            status, payload = _json_bytes({"ok": False, "error": str(exc)}, 400)
            self._send(status, payload)
        except Exception as exc:  # noqa: BLE001
            status, payload = _json_bytes(
                {"ok": False, "error": str(exc), "trace": traceback.format_exc()[-2000:]},
                500,
            )
            self._send(status, payload)

    def _handle_notify_test(self, body: dict[str, Any] | None) -> None:
        if not _authorized(self):
            status, payload = _json_bytes({"ok": False, "error": "unauthorized"}, 401)
            self._send(status, payload)
            return
        body = body or {}
        title = str(body.get("title") or "YiDun 通知测试")
        text = str(body.get("body") or "这是一条来自群晖 YiDun Click Solver 的测试消息。")
        cfg = NOTIFY_CFG
        if not cfg.enabled:
            status, payload = _json_bytes(
                {
                    "ok": False,
                    "error": "notify not configured",
                    "hint": "set SERVERCHAN_SENDKEY / PUSHPLUS_TOKEN / WECOM_WEBHOOK_URL / SMTP_*",
                },
                400,
            )
            self._send(status, payload)
            return
        # Force delivery for explicit test calls.
        forced = NotifyConfig(
            enabled=True,
            on={"always"},
            email_enabled=cfg.email_enabled,
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_user=cfg.smtp_user,
            smtp_password=cfg.smtp_password,
            smtp_from=cfg.smtp_from,
            email_to=list(cfg.email_to),
            smtp_ssl=cfg.smtp_ssl,
            smtp_starttls=cfg.smtp_starttls,
            serverchan_sendkey=cfg.serverchan_sendkey,
            pushplus_token=cfg.pushplus_token,
            wecom_webhook_url=cfg.wecom_webhook_url,
            webhook_url=cfg.webhook_url,
        )
        report = notify_all(forced, ok=True, title=title, body=text, extra={"test": True})
        status, payload = _json_bytes({"ok": bool(report.get("notified")), "notify": report}, 200)
        self._send(status, payload)


def main() -> int:
    if not SOLVER.is_file():
        print(f"solver not found: {SOLVER}", file=sys.stderr)
        return 1
    (ROOT / "captures").mkdir(parents=True, exist_ok=True)
    (ROOT / "results").mkdir(parents=True, exist_ok=True)

    global NOTIFY_CFG
    NOTIFY_CFG = NotifyConfig.from_env()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    auth_state = "enabled" if API_TOKEN else "disabled"
    notify_state = "enabled" if NOTIFY_CFG.enabled else "disabled"
    print(
        f"YiDun NAS API listening on http://{HOST}:{PORT} "
        f"(auth={auth_state}, notify={notify_state})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
