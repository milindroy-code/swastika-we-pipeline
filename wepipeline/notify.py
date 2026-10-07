"""Deliver notifications: always to the dashboard and outbox folder, optionally by email and chat webhook."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

from .context import Ctx, iso
from .db import one

log = logging.getLogger(__name__)
WEBHOOK_KINDS = {"escalation", "eod_report", "morning_note"}


def _send_email(ctx: Ctx, to: str, subject: str, body: str) -> bool:
    cfg = ctx.cfg.get("notifications", {}).get("email", {})
    if not (cfg.get("enabled") and to):
        return False
    msg = EmailMessage()
    msg["From"] = cfg.get("from_address") or cfg.get("username")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=20) as smtp:
            smtp.starttls()
            if cfg.get("username"):
                smtp.login(cfg["username"], os.environ.get(cfg.get("password_env", "SMTP_PASSWORD"), ""))
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.warning("Email to %s failed: %s", to, e)
        return False


def _send_webhook(ctx: Ctx, text: str) -> bool:
    url = ctx.cfg.get("notifications", {}).get("webhook_url")
    if not url:
        return False
    try:
        # "text" is understood by both Slack and Google Chat incoming webhooks.
        requests.post(url, json={"text": text[:3900]}, timeout=10).raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning("Webhook failed: %s", e)
        return False


def notify(ctx: Ctx, recipient: str, kind: str, subject: str, body: str, once_per_day: bool = False) -> bool:
    now = ctx.now()
    if once_per_day and one(
        ctx.conn,
        "SELECT 1 FROM notifications WHERE recipient=? AND subject=? AND substr(created_at,1,10)=?",
        (recipient, subject, now.date().isoformat()),
    ):
        return False

    member = ctx.member(recipient) or {"id": recipient, "name": recipient}
    channels = ["dashboard"]
    try:  # local copy for convenience; hosted deployments have no lasting disk, the database is the record
        outbox = ctx.path(ctx.cfg.get("notifications", {}).get("outbox_dir", "data/outbox")) / now.date().isoformat()
        outbox.mkdir(parents=True, exist_ok=True)
        with open(outbox / f"{recipient}.md", "a", encoding="utf-8") as f:
            f.write(f"\n---\n### {now:%H:%M} · {kind} · {subject}\n\n{body}\n")
    except OSError as e:
        log.warning("Could not write outbox file: %s", e)
    if _send_email(ctx, member.get("email", ""), subject, body):
        channels.append("email")
    if kind in WEBHOOK_KINDS and _send_webhook(ctx, f"*{subject}* ({member['name']})\n{body}"):
        channels.append("webhook")

    ctx.conn.execute(
        "INSERT INTO notifications (kind, recipient, subject, body, created_at, channels) VALUES (?,?,?,?,?,?)",
        (kind, recipient, subject, body, iso(now), ",".join(channels)),
    )
    ctx.conn.commit()
    return True
