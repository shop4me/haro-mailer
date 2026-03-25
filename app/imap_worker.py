import email
import imaplib
import logging
import ssl
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.config import settings
from app.models import InboundEmail, Mailbox

LOGGER = logging.getLogger(__name__)


def _imap_ssl_context(mailbox: Mailbox):
    """SSL context for IMAP; skip verification if mailbox has imap_skip_ssl_verify."""
    if getattr(mailbox, "imap_skip_ssl_verify", False):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def test_mailbox_connection(mailbox: Mailbox, timeout: int = 15) -> tuple[bool, str]:
    """Try to connect and log in to the mailbox. Returns (success, message)."""
    return test_imap_settings(
        imap_host=mailbox.imap_host,
        imap_port=int(mailbox.imap_port),
        imap_user=mailbox.imap_user,
        imap_password=mailbox.imap_password,
        folder=mailbox.folder or "INBOX",
        imap_skip_ssl_verify=bool(getattr(mailbox, "imap_skip_ssl_verify", False)),
        timeout=timeout,
    )


def test_imap_settings(
    imap_host: str,
    imap_port: int,
    imap_user: str,
    imap_password: str,
    folder: str = "INBOX",
    imap_skip_ssl_verify: bool = False,
    timeout: int = 15,
) -> tuple[bool, str]:
    """Test IMAP login and folder access using explicit settings (no DB row required)."""
    class _ConnParams:
        pass

    m = _ConnParams()
    m.imap_host = (imap_host or "").strip()
    m.imap_port = int(imap_port) if imap_port else 993
    m.imap_user = (imap_user or "").strip()
    m.imap_password = imap_password or ""
    m.folder = (folder or "INBOX").strip() or "INBOX"
    m.imap_skip_ssl_verify = bool(imap_skip_ssl_verify)
    try:
        ctx = _imap_ssl_context(m)
        conn = imaplib.IMAP4_SSL(
            m.imap_host, m.imap_port, timeout=timeout, ssl_context=ctx
        )
        conn.login(m.imap_user, m.imap_password)
        conn.select(m.folder)
        conn.close()
        conn.logout()
        return True, "OK"
    except Exception as e:
        return False, _friendly_imap_error(e)


def _friendly_imap_error(exc: Exception) -> str:
    low = str(exc).lower()
    if "timed out" in low or "timeout" in low:
        return "Incoming server did not respond in time. Check IMAP host, port (usually 993), and network."
    if "authentication" in low or ("login" in low and "fail" in low):
        return "Login rejected. Check email and password."
    s = str(exc).strip()
    return s[:400] if len(s) > 400 else s


def get_haro_mailbox(session):
    """Return the single mailbox used for fetching and parsing HARO (only one is used)."""
    # Prefer the mailbox explicitly marked for HARO
    haro = session.scalars(
        select(Mailbox).where(Mailbox.enabled.is_(True), Mailbox.use_for_haro.is_(True)).order_by(Mailbox.id).limit(1)
    ).first()
    if haro:
        return haro
    # Fallback: first enabled mailbox (e.g. before any use_for_haro is set)
    return session.scalars(select(Mailbox).where(Mailbox.enabled.is_(True)).order_by(Mailbox.id).limit(1)).first()


def poll_mailboxes(session) -> int:
    """Fetch and store emails only from the single HARO mailbox (no other mailboxes are polled)."""
    mailbox = get_haro_mailbox(session)
    if not mailbox:
        LOGGER.warning("No HARO mailbox configured; skipping fetch.")
        return 0
    try:
        return _poll_one_mailbox(session, mailbox)
    except Exception as exc:
        LOGGER.exception("HARO mailbox poll failed %s: %s", mailbox.label, exc)
        return 0


def _poll_one_mailbox(session, mailbox: Mailbox) -> int:
    LOGGER.info("Polling mailbox=%s user=%s", mailbox.label, mailbox.imap_user)
    ctx = _imap_ssl_context(mailbox)
    conn = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, ssl_context=ctx)
    conn.login(mailbox.imap_user, mailbox.imap_password)
    conn.select(mailbox.folder)

    msg_ids = set()
    status, unseen = conn.search(None, "UNSEEN")
    if status == "OK":
        msg_ids.update(unseen[0].split())

    since_date = (datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)).strftime("%d-%b-%Y")
    status, recent = conn.search(None, f'(SINCE "{since_date}")')
    if status == "OK":
        msg_ids.update(recent[0].split())

    saved = 0
    for msg_id in sorted(msg_ids):
        status, data = conn.fetch(msg_id, "(RFC822)")
        if status != "OK" or not data:
            continue
        raw_bytes = data[0][1]
        message = email.message_from_bytes(raw_bytes)
        if _store_message(session, mailbox, message):
            saved += 1

    conn.close()
    conn.logout()
    LOGGER.info("Mailbox done=%s saved=%s", mailbox.label, saved)
    return saved


def _store_message(session, mailbox: Mailbox, message: Message) -> bool:
    message_id = (message.get("Message-ID") or "").strip()
    if not message_id:
        return False
    exists = session.scalar(select(InboundEmail).where(InboundEmail.message_id == message_id))
    if exists:
        return False

    from_addr = (message.get("From") or "").strip()
    subject = (message.get("Subject") or "").strip()
    raw_headers = "\n".join(f"{k}: {v}" for (k, v) in message.items())
    body_text, raw_body = _extract_body(message)
    source = _classify_source(from_addr, subject, body_text)
    received_at = _extract_received_dt(message)

    inbound = InboundEmail(
        mailbox_id=mailbox.id,
        message_id=message_id,
        from_addr=from_addr,
        subject=subject,
        received_at=received_at,
        raw_headers=raw_headers,
        raw_body=raw_body,
        body_text=body_text,
        source=source,
        status="STORED",
    )
    session.add(inbound)
    return True


def _extract_body(message: Message) -> tuple[str, str]:
    parts: list[str] = []
    raw_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = (part.get_content_type() or "").lower()
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            raw_parts.append(text)
            if content_type == "text/plain":
                parts.append(text)
            elif content_type == "text/html":
                parts.append(BeautifulSoup(text, "html.parser").get_text("\n"))
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            raw_parts.append(text)
            if message.get_content_type() == "text/html":
                parts.append(BeautifulSoup(text, "html.parser").get_text("\n"))
            else:
                parts.append(text)
    body_text = "\n".join(parts).strip()
    raw_body = "\n".join(raw_parts).strip()
    return body_text, raw_body


def _classify_source(from_addr: str, subject: str, body_text: str) -> str:
    """Tag source for the Inbound list. HARO digests often bury the word HARO deep in HTML, so check From + more body."""
    fa = (from_addr or "").lower()
    if "helpareporter" in fa or "haro@" in fa or ("queries@" in fa and "reporter" in fa):
        return "haro"
    checks = f"{from_addr} {subject} {(body_text or '')[:12000]}".lower()
    if (
        "helpareporter.com" in checks
        or "help a reporter out" in checks
        or "help a reporter" in checks
        or "helpareporter" in checks
        or "haro" in checks
    ):
        return "haro"
    return "unknown"


def _extract_received_dt(message: Message):
    raw = message.get("Date")
    if not raw:
        return datetime.utcnow()
    try:
        return parsedate_to_datetime(raw).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()
