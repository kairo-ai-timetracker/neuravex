"""
Alert dispatch — Telegram/Discord/email. Every channel is independently
failure-isolated: one channel's exception never blocks the others or the
caller. Imports are lazy (inside functions) so a missing optional
dependency doesn't break the whole module at import time.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("neuravex.alerts")


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        import httpx
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message}, timeout=10.0,
        )
        return resp.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("Telegram alert failed: %s", e)
        return False


def send_discord(message: str) -> bool:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False
    try:
        import httpx
        resp = httpx.post(webhook_url, json={"content": message}, timeout=10.0)
        return resp.status_code in (200, 204)
    except Exception as e:  # noqa: BLE001
        logger.warning("Discord alert failed: %s", e)
        return False


def send_email(subject: str, message: str) -> bool:
    smtp_host = os.getenv("ALERT_EMAIL_SMTP_HOST")
    sender = os.getenv("ALERT_EMAIL_FROM")
    recipient = os.getenv("ALERT_EMAIL_TO")
    if not smtp_host or not sender or not recipient:
        return False
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content(message)
        with smtplib.SMTP(smtp_host) as server:
            server.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Email alert failed: %s", e)
        return False


def broadcast_alert(subject: str, message: str) -> dict[str, bool]:
    return {
        "telegram": send_telegram(f"{subject}\n\n{message}"),
        "discord": send_discord(f"**{subject}**\n{message}"),
        "email": send_email(subject, message),
    }
