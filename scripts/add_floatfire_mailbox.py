"""One-off: add Floatfire HARO mailbox and run initial poll."""
import sys
import os

# Run from project root so app and database resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_session, init_db
from app.models import Mailbox
from sqlalchemy import select


def main():
    init_db()
    with get_session() as session:
        existing = session.scalar(select(Mailbox).where(Mailbox.imap_user == "press@floatfire.com"))
        if existing:
            print("Mailbox press@floatfire.com already exists (id=%s). Updating." % existing.id)
            m = existing
        else:
            m = Mailbox()
            session.add(m)
        m.label = "HARO Floatfire"
        m.imap_host = "mail.floatfire.com"
        m.imap_port = 993
        m.imap_user = "press@floatfire.com"
        m.imap_password = "Ur981we@Ur981we@"
        m.folder = "INBOX"
        m.enabled = True
        session.flush()
        print("Mailbox saved: id=%s label=%s" % (m.id, m.label))
    print("Running poll_once to fetch and parse emails...")
    from app.poll_once import main as poll_main
    poll_main()
    print("Done.")


if __name__ == "__main__":
    main()
