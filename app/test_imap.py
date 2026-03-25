"""Test IMAP login for all enabled mailboxes. Run: python -m app.test_imap"""
import sys

from app.db import get_session, init_db
from app.imap_worker import test_mailbox_connection
from app.models import Mailbox
from sqlalchemy import select


def main() -> None:
    init_db()
    all_ok = True
    with get_session() as session:
        mailboxes = session.scalars(select(Mailbox).where(Mailbox.enabled.is_(True))).all()
        if not mailboxes:
            print("No enabled mailboxes found.")
            sys.exit(1)
        for m in mailboxes:
            ok, msg = test_mailbox_connection(m)
            status = "OK" if ok else "FAILED"
            print("%s %s (%s) @ %s:%s — %s" % (status, m.label, m.imap_user, m.imap_host, m.imap_port, msg))
            if not ok:
                all_ok = False
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
