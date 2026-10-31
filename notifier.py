"""Invio email. Usa Resend (API HTTP) se RESEND_API_KEY è impostata,
altrimenti SMTP."""
import os
import smtplib
from email.mime.text import MIMEText
from html import escape

import requests


def _cfg(name, default=None):
    return os.environ.get(name, default)


def email_configured():
    return bool(_cfg("RESEND_API_KEY") or _cfg("SMTP_HOST")) and bool(_cfg("ALERT_EMAIL"))


def send_email(subject, html):
    to = _cfg("ALERT_EMAIL")
    if not to:
        raise RuntimeError("ALERT_EMAIL non impostata")

    if _cfg("RESEND_API_KEY"):
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_cfg('RESEND_API_KEY')}"},
            json={
                "from": _cfg("EMAIL_FROM", "Concorsi Milano <onboarding@resend.dev>"),
                "to": [to],
                "subject": subject,
                "html": html,
            },
            timeout=20,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Resend: {r.status_code} {r.text[:200]}")
        return

    if _cfg("SMTP_HOST"):
        msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = _cfg("EMAIL_FROM", _cfg("SMTP_USER"))
        msg["To"] = to
        port = int(_cfg("SMTP_PORT", "587"))
        with smtplib.SMTP(_cfg("SMTP_HOST"), port, timeout=20) as s:
            s.starttls()
            if _cfg("SMTP_USER"):
                s.login(_cfg("SMTP_USER"), _cfg("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return

    raise RuntimeError("Nessun servizio email configurato (RESEND_API_KEY o SMTP_HOST)")


def bandi_alert(items, source_url):
    """items: lista di dict(title, url, reason, deadline)."""
    n = len(items)
    subject = (f"Nuovo concorso a Milano per diplomati: {items[0]['title'][:90]}"
               if n == 1 else f"{n} nuovi concorsi a Milano per diplomati")
    parts = ['<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5">',
             "<p>Nuovi bandi a Milano che richiedono <b>al massimo il diploma</b>:</p>",
             "<ul style='padding-left:18px'>"]
    for it in items:
        dl = f" · scadenza <b>{escape(it['deadline'])}</b>" if it.get("deadline") else ""
        parts.append(
            f"<li style='margin-bottom:14px'><a href='{escape(it['url'])}'>"
            f"<b>{escape(it['title'])}</b></a>{dl}<br>"
            f"<span style='color:#666;font-size:13px'>{escape(it.get('reason') or '')}</span></li>")
    parts.append("</ul><p style='color:#777;font-size:13px;margin-top:20px'>Concorsi Watch · "
                 f"<a href='{escape(source_url)}'>concorsipubblici.com</a> · "
                 "verifica sempre i requisiti nel bando ufficiale.</p></div>")
    send_email(subject, "".join(parts))
