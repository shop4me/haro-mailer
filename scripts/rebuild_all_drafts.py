#!/usr/bin/env python3
"""Delete reply rows and re-run full classifier + drafter for all stored HARO requests."""
from __future__ import annotations

import argparse
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

from sqlalchemy import delete

from app.db import get_session, init_db
from app.models import Reply
from app.poll_once import reprocess_existing_requests
from app.utils import setup_logging


def main() -> int:
    p = argparse.ArgumentParser(description="Clear replies and rebuild from the current classifier.")
    p.add_argument(
        "--all",
        action="store_true",
        help="Delete every reply (including SENT). You lose send history; use for a full reset.",
    )
    args = p.parse_args()

    setup_logging()
    init_db()
    with get_session() as session:
        if args.all:
            n_del = session.execute(delete(Reply)).rowcount
            print("Removed %s reply row(s) (all statuses)." % (n_del,), flush=True)
        else:
            n_del = session.execute(delete(Reply).where(Reply.send_status == "DRAFT")).rowcount
            print("Removed %s draft reply row(s)." % (n_del,), flush=True)
        n = reprocess_existing_requests(session)
        print("Reprocessed: regenerated %s draft(s) from current classifier." % (n,), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
