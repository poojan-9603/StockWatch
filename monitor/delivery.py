"""Alert delivery: email over SMTP and Discord by webhook, equal channels (full reports go by email).

Environment: SMTP_HOST, SMTP_PORT (465 is SSL, anything else STARTTLS), SMTP_USER, SMTP_PASSWORD (an app password),
ALERT_EMAIL_TO, DISCORD_WEBHOOK_URL. Each send tries 3 times with backoff, then raises. Errors and logs never carry
the password or the webhook URL.
"""
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

import requests

TRIES = 3
DISCORD_LIMIT = 2000


def retry(what, fn, secrets):
    for attempt in range(TRIES):
        try:
            return fn()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            for s in filter(None, secrets):
                err = err.replace(s, "***")
            print(f"{what} failed (try {attempt + 1}/{TRIES}): {err}", file=sys.stderr)
            if attempt == TRIES - 1:
                raise RuntimeError(f"{what} failed after {TRIES} tries: {err}") from None
            time.sleep(2 ** attempt * 5)


def send_email(subject, body):
    env = os.environ
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, env["SMTP_USER"], env["ALERT_EMAIL_TO"]
    msg.set_content(body)
    port = int(env["SMTP_PORT"])

    def go():
        with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(env["SMTP_HOST"], port, timeout=30) as s:
            if port != 465:
                s.starttls()
            s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            s.send_message(msg)

    retry("email", go, [env["SMTP_PASSWORD"]])


def send_discord(text):
    url = os.environ["DISCORD_WEBHOOK_URL"]

    def post(chunk):
        r = requests.post(url, json={"content": chunk}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 1)))
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code}")

    for i in range(0, len(text), DISCORD_LIMIT):
        retry("discord", lambda: post(text[i:i + DISCORD_LIMIT]), [url, urlsplit(url).path])


def deliver(channel, msg):
    """msg: {subject, body} from monitor.alerts.message."""
    if channel == "email":
        send_email(msg["subject"], msg["body"])
    elif channel == "app":
        return  # nothing to send: the alerts table is the delivery, and the site's Today screen shows it
    elif channel == "discord":
        send_discord(f"**{msg['subject']}**\n{msg['body']}")
    else:
        raise ValueError(f"unknown channel {channel}")
