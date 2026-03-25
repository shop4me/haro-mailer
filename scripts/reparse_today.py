#!/usr/bin/env python3
"""
Delete parsed HARO requests (and their classifications/replies) for inbound mail received
on the current UTC calendar day, reset those emails to unprocessed, and run the parser
+ classifier + drafter again (same path as a normal poll process step).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sqlalchemy import delete, select

from app.db import get_session, init_db
from app.models import Classification, HaroRequest, InboundEmail, Reply
from app.poll_once import process_pending_haro
from app.utils import setup_logging


def _utc_day_bounds(when: datetime | None = None) -> tuple[datetime, datetime]:
    now = when or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)
    return start, end


def main() -> int:
    p = argparse.ArgumentParser(description="Reset today's parsed requests and reprocess inbound mail.")
    p.add_argument(
        "--date",
        help="UTC date YYYY-MM-DD (default: today UTC).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without changing the database.",
    )
    args = p.parse_args()

    setup_logging()
    init_db()

    if args.date:
        y, m, d = (int(x) for x in args.date.split("-"))
        start, end = _utc_day_bounds(datetime(y, m, d))
    else:
        start, end = _utc_day_bounds()

    print(
        "UTC window: %s .. %s (exclusive end)"
        % (start.isoformat() + "Z", end.isoformat() + "Z"),
        flush=True,
    )

    with get_session() as session:
        inbounds = session.scalars(
            select(InboundEmail).where(
                InboundEmail.received_at >= start,
                InboundEmail.received_at < end,
            )
        ).all()

        if not inbounds:
            print("No inbound emails in this window; nothing to reset.", flush=True)
            return 0

        inbound_ids = [r.id for r in inbounds]
        req_rows = session.scalars(
            select(HaroRequest).where(HaroRequest.inbound_email_id.in_(inbound_ids))
        ).all()
        req_ids = [r.id for r in req_rows]

        print(
            "Found %s inbound row(s), %s haro_request row(s) to remove."
            % (len(inbound_ids), len(req_ids)),
            flush=True,
        )

        if args.dry_run:
            for ib in inbounds:
                print("  inbound id=%s subject=%r" % (ib.id, (ib.subject or "")[:80]), flush=True)
            return 0

        if req_ids:
            n_rep = session.execute(delete(Reply).where(Reply.haro_request_id.in_(req_ids))).rowcount
            n_cls = session.execute(
                delete(Classification).where(Classification.haro_request_id.in_(req_ids))
            ).rowcount
            n_req = session.execute(delete(HaroRequest).where(HaroRequest.id.in_(req_ids))).rowcount
            print(
                "Deleted replies=%s classifications=%s haro_requests=%s"
                % (n_rep, n_cls, n_req),
                flush=True,
            )

        for ib in inbounds:
            ib.processed_at = None
            ib.status = "NEW"

        # Required so process_pending_haro's SELECT ... WHERE processed_at IS NULL sees the reset
        # (otherwise the DB still has old timestamps until flush).
        session.flush()

        print("Reset processed_at/status for %s inbound row(s)." % len(inbounds), flush=True)

        processed = process_pending_haro(session)
        print("process_pending_haro: processed=%s request pipeline step(s)." % processed, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
