import json
import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import select

from app.config import settings
from app.models import Business, InboundEmail, Mailbox, Reply

LOGGER = logging.getLogger(__name__)

# Outbound mail is only the drafted reply body plus optional image attachments.
# Asset metadata stays in the dashboard (request detail), never appended for journalists.


def reply_attachment_paths(reply: Reply) -> list[str]:
    """Paths from the reply row that exist on disk (for MIME attachments)."""
    raw = getattr(reply, "inline_preview_paths_json", None) or ""
    if not raw:
        return []
    try:
        return [p for p in json.loads(raw) if p and os.path.isfile(str(p))]
    except (json.JSONDecodeError, TypeError, OSError):
        return []


def smtp_mailbox_for_reply(session, business: Business, inbound_email: InboundEmail | None) -> Mailbox | None:
    """SMTP for replies comes only from the business's linked mailbox (configure SMTP on the Mailboxes page)."""
    _ = inbound_email  # kept for call-site compatibility
    mid = getattr(business, "mailbox_id", None)
    if not mid:
        return None
    mb = session.scalar(select(Mailbox).where(Mailbox.id == mid))
    if mb and (mb.smtp_host or "").strip():
        return mb
    if mb:
        LOGGER.warning(
            "Business id=%s mailbox id=%s has no SMTP host configured on the mailbox.",
            business.id,
            mid,
        )
    return None


def resolve_destination(
    haro_reply_to: str | None, inbound_email: InboundEmail | None, request_text: str
) -> str | None:
    if haro_reply_to:
        return haro_reply_to.strip()
    if not inbound_email:
        return _parse_instructions_email(request_text)
    header_reply_to = _header_lookup(inbound_email.raw_headers, "Reply-To")
    if header_reply_to:
        parsed = parseaddr(header_reply_to)[1]
        if parsed:
            return parsed
    parsed_from_text = _parse_instructions_email(request_text)
    if parsed_from_text:
        return parsed_from_text
    return None


def _ssl_default_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _alternate_submission_port(port: int) -> int | None:
    """If submission fails, try the other common port (587 STARTTLS vs 465 SSL)."""
    port = int(port) if port else 587
    if port == 587:
        return 465
    if port == 465:
        return 587
    return None


def _smtp_failover_eligible(exc: Exception) -> bool:
    """True if retrying on the alternate port might help (TLS/connect issues, not bad password)."""
    low = str(exc).lower()
    if "authentication" in low or "535" in low:
        return False
    if ("login" in low or "password" in low) and ("fail" in low or "invalid" in low):
        return False
    return any(
        x in low
        for x in (
            "timed out",
            "timeout",
            "unexpectedly closed",
            "connection reset",
            "connection refused",
            "eof occurred",
            "broken pipe",
            "ssl:",
            "wrong version number",
            "certificate",
        )
    )


def _friendly_smtp_error(exc: Exception) -> str:
    low = str(exc).lower()
    if "timed out" in low or "timeout" in low:
        return (
            "Outgoing server did not respond in time. "
            "Confirm SMTP host and port: use 465 for SSL or 587 for STARTTLS, "
            "and check firewalls or VPN."
        )
    if "connection refused" in low:
        return "Could not reach the outgoing mail server (connection refused). Check SMTP host and port."
    if "unexpectedly closed" in low:
        return (
            "Outgoing connection closed early—often wrong port or TLS mode. "
            "Port 465 needs SSL; port 587 uses STARTTLS. Try the other if your provider lists both."
        )
    if "authentication" in low or "535" in low or ("auth" in low and "fail" in low):
        return "Login rejected by the outgoing mail server. Check email and password."
    s = str(exc).strip()
    return s[:400] if len(s) > 400 else s


def _smtp_session(host: str, port: int, user: str, password: str, timeout: int, after_login):
    """Port 465 = implicit SSL; other ports = plain + STARTTLS. Calls after_login(smtp) after login."""
    port = int(port) if port else 587
    user = user or ""
    password = password or ""
    ctx = _ssl_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as smtp:
            smtp.login(user, password)
            after_login(smtp)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(user, password)
            after_login(smtp)


def test_smtp_settings(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    timeout: int = 30,
) -> tuple[bool, str]:
    """Try SMTP login. Port 465 = SSL; 587/others = STARTTLS. Retries the alternate port on TLS/connect issues."""
    host = (smtp_host or "").strip()
    if not host:
        return False, "SMTP host is required."
    port = int(smtp_port) if smtp_port else 587
    try:
        _smtp_session(host, port, smtp_user, smtp_password, timeout=timeout, after_login=lambda _s: None)
        return True, "OK"
    except Exception as e:
        if not _smtp_failover_eligible(e):
            return False, _friendly_smtp_error(e)
        alt = _alternate_submission_port(port)
        if alt is None:
            return False, _friendly_smtp_error(e)
        try:
            _smtp_session(host, alt, smtp_user, smtp_password, timeout=timeout, after_login=lambda _s: None)
            return True, "OK (used port %s — save this SMTP port in Mailboxes)" % alt
        except Exception as e2:
            return False, _friendly_smtp_error(e2)


def send_reply(
    reply: Reply,
    destination_email: str,
    business: Business,
    inbound_email: InboundEmail | None = None,
    smtp_mailbox: Mailbox | None = None,
    attachment_paths: list[str] | None = None,
) -> tuple[bool, str]:
    if reply.send_status == "SENT":
        return False, "Reply already sent"
    if not smtp_mailbox or not (smtp_mailbox.smtp_host or "").strip():
        return (
            False,
            "This business has no mailbox with SMTP configured. Link a mailbox under Businesses and set SMTP under Mailboxes.",
        )
    smtp_user = (smtp_mailbox.imap_user or smtp_mailbox.smtp_user or "").strip()
    smtp_password = smtp_mailbox.imap_password or smtp_mailbox.smtp_password or ""
    if not smtp_user:
        return False, "The linked mailbox has no email address for login."
    from_addr = smtp_user
    msg = EmailMessage()
    msg["Subject"] = reply.reply_subject
    msg["From"] = from_addr
    msg["To"] = destination_email
    if settings.reply_bcc_email:
        msg["Bcc"] = settings.reply_bcc_email
        LOGGER.info("smtp reply id=%s bcc copy enabled", reply.id)
    if inbound_email:
        if inbound_email.message_id:
            msg["In-Reply-To"] = inbound_email.message_id
            msg["References"] = inbound_email.message_id
    msg.set_content(reply.reply_body or "")

    paths = attachment_paths or []
    if settings.enable_inline_image_previews and paths:
        max_n = max(1, settings.max_inline_preview_images)
        for path in paths[:max_n]:
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            lower = path.lower()
            if lower.endswith((".jpg", ".jpeg")):
                sub = "jpeg"
            elif lower.endswith(".png"):
                sub = "png"
            elif lower.endswith(".webp"):
                sub = "webp"
            else:
                sub = "jpeg"
            msg.add_attachment(
                data,
                maintype="image",
                subtype=sub,
                filename=os.path.basename(path),
            )
            LOGGER.info("smtp attachment filename=%s bytes=%s", os.path.basename(path), len(data))

    smtp_host = (smtp_mailbox.smtp_host or "").strip()
    smtp_port = smtp_mailbox.smtp_port
    port = int(smtp_port) if smtp_port else 587
    try:
        _smtp_session(
            smtp_host,
            port,
            smtp_user,
            smtp_password,
            timeout=30,
            after_login=lambda s: s.send_message(msg),
        )
        reply.send_status = "SENT"
        reply.sent_at = datetime.utcnow()
        reply.error_message = None
        reply.smtp_response = "OK"
        LOGGER.info("Sent reply id=%s to=%s", reply.id, destination_email)
        return True, "OK"
    except Exception as exc:
        alt = _alternate_submission_port(port)
        if alt is not None and _smtp_failover_eligible(exc):
            try:
                LOGGER.warning("SMTP retry id=%s on port %s after: %s", reply.id, alt, exc)
                _smtp_session(
                    smtp_host,
                    alt,
                    smtp_user,
                    smtp_password,
                    timeout=30,
                    after_login=lambda s: s.send_message(msg),
                )
                reply.send_status = "SENT"
                reply.sent_at = datetime.utcnow()
                reply.error_message = None
                reply.smtp_response = "OK"
                LOGGER.info("Sent reply id=%s via fallback port %s", reply.id, alt)
                return True, "OK"
            except Exception as exc2:
                exc = exc2
        reply.send_status = "FAILED"
        reply.error_message = str(exc)
        reply.smtp_response = "ERROR"
        LOGGER.exception("SMTP send failed reply id=%s", reply.id)
        return False, _friendly_smtp_error(exc)


def _header_lookup(headers_text: str, name: str) -> str | None:
    for line in headers_text.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _parse_instructions_email(text: str) -> str | None:
    lowered = text.lower()
    markers = ["send responses to:", "email:", "reply to:"]
    for marker in markers:
        idx = lowered.find(marker)
        if idx >= 0:
            segment = text[idx : idx + 200]
            parts = segment.replace("\n", " ").split()
            for p in parts:
                if "@" in p and "." in p:
                    return p.strip(" ,.;<>")
    return None
