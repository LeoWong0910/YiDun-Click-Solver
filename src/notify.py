"""Notification helpers for NAS deploy: email + WeChat-style push channels.

Supported channels (any combination):
  - email       SMTP (ssl or starttls)
  - serverchan  Server酱 turbo  https://sct.ftqq.com
  - pushplus    PushPlus         http://www.pushplus.plus
  - wecom       WeCom group bot webhook
  - webhook     Generic JSON webhook POST
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib import error, request


@dataclass
class NotifyConfig:
    enabled: bool = False
    on: set[str] = field(default_factory=lambda: {"success", "failure"})

    # Email / SMTP
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    email_to: list[str] = field(default_factory=list)
    smtp_ssl: bool = True
    smtp_starttls: bool = False

    # WeChat-friendly push
    serverchan_sendkey: str = ""
    pushplus_token: str = ""
    wecom_webhook_url: str = ""
    webhook_url: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "NotifyConfig":
        e = env if env is not None else os.environ
        on_raw = (e.get("NOTIFY_ON") or "success,failure").strip()
        on = {part.strip().lower() for part in on_raw.split(",") if part.strip()}
        if not on:
            on = {"success", "failure"}

        email_to_raw = e.get("NOTIFY_EMAIL_TO") or e.get("SMTP_TO") or ""
        email_to = [x.strip() for x in email_to_raw.split(",") if x.strip()]
        smtp_host = (e.get("SMTP_HOST") or "").strip()
        smtp_from = (e.get("SMTP_FROM") or e.get("SMTP_USER") or "").strip()
        email_enabled = _truthy(e.get("NOTIFY_EMAIL")) or bool(smtp_host and email_to)

        serverchan = (e.get("SERVERCHAN_SENDKEY") or e.get("NOTIFY_SERVERCHAN_SENDKEY") or "").strip()
        pushplus = (e.get("PUSHPLUS_TOKEN") or e.get("NOTIFY_PUSHPLUS_TOKEN") or "").strip()
        wecom = (e.get("WECOM_WEBHOOK_URL") or e.get("NOTIFY_WECOM_WEBHOOK") or "").strip()
        webhook = (e.get("NOTIFY_WEBHOOK_URL") or e.get("WEBHOOK_URL") or "").strip()

        enabled_flag = e.get("NOTIFY_ENABLED")
        if enabled_flag is None or enabled_flag == "":
            enabled = bool(email_enabled or serverchan or pushplus or wecom or webhook)
        else:
            enabled = _truthy(enabled_flag)

        port = int(e.get("SMTP_PORT") or "465")
        ssl_flag = e.get("SMTP_SSL")
        starttls_flag = e.get("SMTP_STARTTLS")
        smtp_ssl = _truthy(ssl_flag) if ssl_flag not in (None, "") else port == 465
        smtp_starttls = _truthy(starttls_flag) if starttls_flag not in (None, "") else (port == 587)

        return cls(
            enabled=enabled,
            on=on,
            email_enabled=email_enabled,
            smtp_host=smtp_host,
            smtp_port=port,
            smtp_user=(e.get("SMTP_USER") or "").strip(),
            smtp_password=(e.get("SMTP_PASSWORD") or "").strip(),
            smtp_from=smtp_from,
            email_to=email_to,
            smtp_ssl=smtp_ssl,
            smtp_starttls=smtp_starttls,
            serverchan_sendkey=serverchan,
            pushplus_token=pushplus,
            wecom_webhook_url=wecom,
            webhook_url=webhook,
        )


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def should_notify(cfg: NotifyConfig, *, ok: bool) -> bool:
    if "always" in cfg.on:
        return True
    if ok and "success" in cfg.on:
        return True
    if (not ok) and "failure" in cfg.on:
        return True
    return False


def build_solve_message(
    *,
    ok: bool,
    result: dict[str, Any] | None = None,
    extra: str = "",
) -> tuple[str, str]:
    result = result or {}
    title = "YiDun 验证码识别成功" if ok else "YiDun 验证码识别失败"
    lines = [
        f"状态: {'成功' if ok else '失败'}",
        f"token: {result.get('token') or '-'}",
        f"validate: {result.get('validate') or '-'}",
        f"points: {result.get('points') or '-'}",
        f"zoneId: {result.get('zoneId') or '-'}",
        f"boundary: {result.get('boundary') or '-'}",
    ]
    if extra:
        lines.append(extra)
    return title, "\n".join(lines)


def _http_json(
    url: str,
    *,
    method: str = "POST",
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> Any:
    headers = {"User-Agent": "YiDunClickSolver-Notify/1.0"}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif form_body is not None:
        from urllib.parse import urlencode

        data = urlencode(form_body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw[:500]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc

    raw = raw.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def send_email(cfg: NotifyConfig, title: str, body: str) -> tuple[bool, str]:
    if not (cfg.email_enabled and cfg.smtp_host and cfg.email_to and cfg.smtp_from):
        return False, "email: not configured"
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(cfg.email_to)
    msg.set_content(body)

    try:
        if cfg.smtp_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=20) as client:
                if cfg.smtp_user:
                    client.login(cfg.smtp_user, cfg.smtp_password)
                client.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as client:
                client.ehlo()
                if cfg.smtp_starttls:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if cfg.smtp_user:
                    client.login(cfg.smtp_user, cfg.smtp_password)
                client.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        return False, f"email: error: {exc}"
    return True, "email: smtp sent"


def send_serverchan(cfg: NotifyConfig, title: str, body: str) -> tuple[bool, str]:
    if not cfg.serverchan_sendkey:
        return False, "serverchan: not configured"
    url = f"https://sctapi.ftqq.com/{cfg.serverchan_sendkey}.send"
    try:
        resp = _http_json(url, form_body={"title": title[:32], "desp": body})
    except Exception as exc:  # noqa: BLE001
        return False, f"serverchan: error: {exc}"
    code = resp.get("code") if isinstance(resp, dict) else None
    if code not in (0, "0", None) and resp.get("data") is None:
        # Some responses use errno / message without code=0 on soft failures.
        if isinstance(resp, dict) and resp.get("message") and code not in (0, "0"):
            return False, f"serverchan: rejected: {resp}"
    return True, f"serverchan: ok ({resp})"


def send_pushplus(cfg: NotifyConfig, title: str, body: str) -> tuple[bool, str]:
    if not cfg.pushplus_token:
        return False, "pushplus: not configured"
    payload = {
        "token": cfg.pushplus_token,
        "title": title,
        "content": body.replace("\n", "<br>"),
        "template": "html",
    }
    try:
        resp = _http_json("http://www.pushplus.plus/send", json_body=payload)
    except Exception as exc:  # noqa: BLE001
        return False, f"pushplus: error: {exc}"
    code = resp.get("code") if isinstance(resp, dict) else None
    if code not in (200, "200", 0, "0", None):
        return False, f"pushplus: rejected: {resp}"
    return True, f"pushplus: ok ({resp})"


def send_wecom(cfg: NotifyConfig, title: str, body: str) -> tuple[bool, str]:
    if not cfg.wecom_webhook_url:
        return False, "wecom: not configured"
    content = f"{title}\n{body}"
    payload = {"msgtype": "text", "text": {"content": content[:2000]}}
    try:
        resp = _http_json(cfg.wecom_webhook_url, json_body=payload)
    except Exception as exc:  # noqa: BLE001
        return False, f"wecom: error: {exc}"
    errcode = resp.get("errcode") if isinstance(resp, dict) else None
    if errcode not in (0, "0", None):
        return False, f"wecom: rejected: {resp}"
    return True, f"wecom: ok ({resp})"


def send_webhook(
    cfg: NotifyConfig,
    title: str,
    body: str,
    *,
    extra: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if not cfg.webhook_url:
        return False, "webhook: not configured"
    payload = {"title": title, "body": body, "source": "yidun-click-solver"}
    if extra:
        payload.update(extra)
    try:
        resp = _http_json(cfg.webhook_url, json_body=payload)
    except Exception as exc:  # noqa: BLE001
        return False, f"webhook: error: {exc}"
    return True, f"webhook: ok ({resp})"


def notify_all(
    cfg: NotifyConfig,
    *,
    ok: bool,
    title: str,
    body: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not cfg.enabled:
        return {"notified": False, "skipped": "disabled", "channels": []}
    if not should_notify(cfg, ok=ok):
        return {"notified": False, "skipped": "filtered", "channels": []}

    channels: list[dict[str, Any]] = []

    def _run(name: str, fn: Any) -> None:
        success, detail = fn()
        channels.append({"channel": name, "ok": success, "detail": detail})

    if cfg.email_enabled:
        _run("email", lambda: send_email(cfg, title, body))
    if cfg.serverchan_sendkey:
        _run("serverchan", lambda: send_serverchan(cfg, title, body))
    if cfg.pushplus_token:
        _run("pushplus", lambda: send_pushplus(cfg, title, body))
    if cfg.wecom_webhook_url:
        _run("wecom", lambda: send_wecom(cfg, title, body))
    if cfg.webhook_url:
        _run("webhook", lambda: send_webhook(cfg, title, body, extra=extra))

    if not channels:
        return {"notified": False, "skipped": "no_channels", "channels": []}
    return {
        "notified": any(item["ok"] for item in channels),
        "channels": channels,
    }
