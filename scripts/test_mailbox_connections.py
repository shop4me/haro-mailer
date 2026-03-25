#!/usr/bin/env python3
"""Test IMAP + SMTP for every mailbox in the database. Run from project root with DATABASE_URL set."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sqlalchemy import select

from app.db import get_session
from app.imap_worker import test_imap_settings
from app.models import Mailbox
from app.smtp_sender import test_smtp_settings


def main() -> int:
    try:
        with get_session() as db:
            rows = db.scalars(select(Mailbox).order_by(Mailbox.id)).all()
    except Exception as e:
        print("Database error:", e)
        return 1
    if not rows:
        print("No mailboxes in database.")
        return 0
    any_fail = False
    for m in rows:
        print("=== Mailbox id=%s label=%r ===" % (m.id, m.label))
        ok, msg = test_imap_settings(
            m.imap_host,
            m.imap_port,
            m.imap_user,
            m.imap_password,
            m.folder or "INBOX",
            bool(getattr(m, "imap_skip_ssl_verify", False)),
            timeout=25,
        )
        print("  IMAP:", "OK" if ok else "FAIL", "-", msg)
        if not ok:
            any_fail = True
        if (m.smtp_host or "").strip():
            ok2, msg2 = test_smtp_settings(
                m.smtp_host,
                m.smtp_port or 587,
                m.imap_user,
                m.imap_password,
                timeout=30,
            )
            print("  SMTP:", "OK" if ok2 else "FAIL", "-", msg2)
            if not ok2:
                any_fail = True
        else:
            print("  SMTP: (not configured)")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
