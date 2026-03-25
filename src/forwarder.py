"""
Forwards a scraped Message to a Gmail address via smtplib + TLS.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.scraper import Message

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def forward_message(
    msg: Message,
    *,
    smtp_user: str,
    smtp_password: str,
    target_address: str,
) -> None:
    """
    Build a MIME email that preserves the original HTML body and
    adds a forwarding header, then sends it via Gmail SMTP.
    """
    mime = MIMEMultipart("alternative")
    mime["Subject"] = f"[HU-FWD] {msg.subject}"
    mime["From"] = smtp_user
    mime["To"] = target_address
    mime["X-Forwarded-From"] = f"{msg.sender_name} <{msg.sender_email}>"
    mime["X-Original-Date"] = msg.date

    # Plain-text fallback
    text_body = (
        f"From   : {msg.sender_name} <{msg.sender_email}>\n"
        f"Date   : {msg.date}\n"
        f"Subject: {msg.subject}\n"
        f"{'-'*60}\n\n"
        f"{msg.body_text}"
    )

    # HTML version with a forwarding banner
    html_banner = (
        "<div style='background:#f4f4f4;border-left:4px solid #d00;padding:8px 12px;"
        "margin-bottom:16px;font-family:sans-serif;font-size:13px;color:#333'>"
        f"<b>Forwarded from Hacettepe Mail</b><br>"
        f"<b>From:</b> {msg.sender_name} &lt;{msg.sender_email}&gt;<br>"
        f"<b>Date:</b> {msg.date}<br>"
        f"<b>Subject:</b> {msg.subject}"
        "</div>"
    )
    html_body = f"<html><body>{html_banner}{msg.body_html}</body></html>"

    mime.attach(MIMEText(text_body, "plain", "utf-8"))
    mime.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info(
        "Forwarding '%s' → %s via %s:%s", msg.subject, target_address, SMTP_HOST, SMTP_PORT
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [target_address], mime.as_bytes())
    logger.info("Forwarded successfully: %s", msg.subject)
