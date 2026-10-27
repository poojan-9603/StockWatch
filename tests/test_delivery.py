import pytest
import requests

from monitor import delivery

HOOK = "https://discord.com/api/webhooks/123/SECRET-TOKEN"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465", "SMTP_USER": "me@example.com",
                 "SMTP_PASSWORD": "app-pass-123", "ALERT_EMAIL_TO": "you@example.com", "DISCORD_WEBHOOK_URL": HOOK}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(delivery.time, "sleep", lambda s: None)


class FakeSMTP:
    calls, fail = [], 0

    def __init__(self, host, port, timeout):
        self.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.fail:
            FakeSMTP.fail -= 1
            raise OSError(f"login refused for {user} with {password}")
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["Subject"], msg["To"], msg.get_content().strip()))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.calls, FakeSMTP.fail = [], 0
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(delivery.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_email_over_ssl_and_starttls(smtp, monkeypatch):
    delivery.deliver("email", {"subject": "StockWatch: 1 urgent", "body": "full report"})
    assert smtp.calls == [("connect", "smtp.example.com", 465), ("login", "me@example.com"),
                          ("send", "StockWatch: 1 urgent", "you@example.com", "full report")]
    smtp.calls.clear()
    monkeypatch.setenv("SMTP_PORT", "587")
    delivery.send_email("s", "b")
    assert smtp.calls[:2] == [("connect", "smtp.example.com", 587), ("starttls",)]


def test_email_retries_then_raises_without_the_password(smtp, capsys):
    smtp.fail = 2
    delivery.send_email("s", "b")  # third try works
    assert smtp.calls[-1][0] == "send"
    smtp.fail = 3
    with pytest.raises(RuntimeError, match="email failed after 3 tries") as e:
        delivery.send_email("s", "b")
    assert "app-pass-123" not in str(e.value) and "app-pass-123" not in capsys.readouterr().err


def test_missing_settings_fail_loudly(smtp, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    with pytest.raises(KeyError, match="DISCORD_WEBHOOK_URL"):
        delivery.send_discord("x")


class Resp:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}


def test_discord_splits_long_messages_and_waits_on_rate_limit(monkeypatch):
    posts, answers, slept = [], [Resp(204), Resp(429, {"Retry-After": "2.5"}), Resp(204), Resp(204)], []
    monkeypatch.setattr(delivery.time, "sleep", slept.append)
    monkeypatch.setattr(delivery.requests, "post", lambda url, json, timeout: posts.append(json["content"]) or answers.pop(0))
    delivery.deliver("discord", {"subject": "S", "body": "x" * 4500})
    assert [len(p) for p in posts] == [2000, 2000, 2000, 506]  # the second chunk went twice
    assert posts[1] == posts[2] and "".join(posts[:1] + posts[2:]) == "**S**\n" + "x" * 4500
    assert 2.5 in slept


def test_discord_failure_never_shows_the_webhook(monkeypatch, capsys):
    def boom(url, json, timeout):
        raise requests.ConnectionError(f"Max retries exceeded with url: /api/webhooks/123/SECRET-TOKEN ({url})")

    monkeypatch.setattr(delivery.requests, "post", boom)
    with pytest.raises(RuntimeError, match="discord failed after 3 tries") as e:
        delivery.send_discord("hello")
    shown = str(e.value) + capsys.readouterr().err
    assert "SECRET-TOKEN" not in shown and "ConnectionError" in shown
    assert e.value.__suppress_context__

    monkeypatch.setattr(delivery.requests, "post", lambda url, json, timeout: Resp(404))
    with pytest.raises(RuntimeError, match="HTTP 404"):
        delivery.send_discord("hello")


def test_unknown_channel():
    with pytest.raises(ValueError):
        delivery.deliver("sms", {"subject": "s", "body": "b"})
