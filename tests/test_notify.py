#!/usr/bin/env python3
"""Unit tests for notification helpers (email / WeChat-style push)."""

from __future__ import annotations

import smtplib
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.notify import (  # noqa: E402
    NotifyConfig,
    build_solve_message,
    notify_all,
    send_email,
    send_pushplus,
    send_serverchan,
    send_webhook,
    should_notify,
)


class ShouldNotifyTests(unittest.TestCase):
    def test_success_only(self) -> None:
        cfg = NotifyConfig(on={"success"})
        self.assertTrue(should_notify(cfg, ok=True))
        self.assertFalse(should_notify(cfg, ok=False))

    def test_failure_only(self) -> None:
        cfg = NotifyConfig(on={"failure"})
        self.assertFalse(should_notify(cfg, ok=True))
        self.assertTrue(should_notify(cfg, ok=False))

    def test_always(self) -> None:
        cfg = NotifyConfig(on={"always"})
        self.assertTrue(should_notify(cfg, ok=True))
        self.assertTrue(should_notify(cfg, ok=False))


class MessageTests(unittest.TestCase):
    def test_build_solve_message_success(self) -> None:
        title, body = build_solve_message(
            ok=True,
            result={"validate": "CN31_x", "token": "abc", "points": [[1, 2, 0]]},
        )
        self.assertIn("成功", title)
        self.assertIn("CN31_x", body)
        self.assertIn("abc", body)


class ChannelTests(unittest.TestCase):
    def test_send_email_uses_smtp(self) -> None:
        cfg = NotifyConfig(
            email_enabled=True,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="u@example.com",
            smtp_password="secret",
            smtp_from="u@example.com",
            email_to=["a@example.com"],
            smtp_ssl=True,
        )
        with mock.patch("src.notify.smtplib.SMTP_SSL") as smtp_ssl:
            client = mock.MagicMock()
            smtp_ssl.return_value.__enter__.return_value = client
            ok, detail = send_email(cfg, "t", "b")
        self.assertTrue(ok)
        client.login.assert_called_once()
        client.send_message.assert_called_once()
        self.assertIn("smtp", detail)

    def test_send_email_requires_config(self) -> None:
        ok, detail = send_email(NotifyConfig(), "t", "b")
        self.assertFalse(ok)
        self.assertIn("not configured", detail)

    def test_serverchan_posts_sendkey(self) -> None:
        cfg = NotifyConfig(serverchan_sendkey="SCT_test")
        fake = mock.Mock(return_value={"code": 0, "message": "ok"})
        with mock.patch("src.notify._http_json", fake):
            ok, detail = send_serverchan(cfg, "hello", "world")
        self.assertTrue(ok)
        self.assertIn("serverchan", detail)
        args, kwargs = fake.call_args
        self.assertIn("SCT_test", args[0])

    def test_pushplus_posts_token(self) -> None:
        cfg = NotifyConfig(pushplus_token="tok")
        fake = mock.Mock(return_value={"code": 200})
        with mock.patch("src.notify._http_json", fake):
            ok, detail = send_pushplus(cfg, "hello", "world")
        self.assertTrue(ok)
        payload = fake.call_args.kwargs.get("json_body")
        self.assertEqual(payload["token"], "tok")

    def test_webhook_posts_json(self) -> None:
        cfg = NotifyConfig(webhook_url="https://example.com/hook")
        fake = mock.Mock(return_value={"ok": True})
        with mock.patch("src.notify._http_json", fake):
            ok, detail = send_webhook(cfg, "hello", "world", extra={"ok": True})
        self.assertTrue(ok)
        self.assertIn("webhook", detail)

    def test_notify_all_skips_when_disabled(self) -> None:
        cfg = NotifyConfig(enabled=False, on={"always"})
        report = notify_all(cfg, ok=True, title="t", body="b")
        self.assertEqual(report["skipped"], "disabled")

    def test_notify_all_runs_enabled_channels(self) -> None:
        cfg = NotifyConfig(
            enabled=True,
            on={"success"},
            serverchan_sendkey="SCT_x",
            pushplus_token="tok",
        )
        with mock.patch("src.notify.send_serverchan", return_value=(True, "serverchan:ok")) as sc, mock.patch(
            "src.notify.send_pushplus", return_value=(True, "pushplus:ok")
        ) as pp:
            report = notify_all(cfg, ok=True, title="t", body="b")
        self.assertTrue(report["notified"])
        sc.assert_called_once()
        pp.assert_called_once()
        self.assertEqual(len(report["channels"]), 2)


if __name__ == "__main__":
    unittest.main()
