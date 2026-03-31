#!/usr/bin/env python3
"""Find HARO requests from the last N days that match home & garden policy; create/update draft replies."""
from __future__ import annotations

import argparse
import json
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

from sqlalchemy import select

from app.classifier import classify_request
from app.db import get_session, init_db
from app.drafter import draft_reply
from app.models import Business, Classification, HaroRequest, InboundEmail, Reply


def main() -> int:
    p = argparse.ArgumentParser(description="Draft replies for home & garden requests in a date window.")
    p.add_argument("--days", type=int, default=5, help="Look back this many days (default 5).")
    args = p.parse_args()

    init_db()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)

    drafted = 0
    skipped_sent = 0
    skipped_not_hg = 0
    skipped_no_match = 0
    errors = 0

    with get_session() as session:
        businesses = session.scalars(select(Business).where(Business.enabled.is_(True))).all()
        if not businesses:
            print("No enabled businesses. Add one under Businesses.")
            return 1

        reqs = session.scalars(
            select(HaroRequest)
            .join(InboundEmail, HaroRequest.inbound_email_id == InboundEmail.id)
            .where(InboundEmail.received_at >= cutoff)
            .order_by(HaroRequest.id)
        ).all()

        print("Cutoff (UTC): %s — %s request(s) in window." % (cutoff.isoformat(), len(reqs)))

        for req in reqs:
            try:
                inbound_row = session.scalar(
                    select(InboundEmail).where(InboundEmail.id == req.inbound_email_id)
                )
                match = classify_request(
                    req, businesses, inbound_source=inbound_row.source if inbound_row else None
                )
                tags = match.topic_tags or []
                if "home_garden" not in tags:
                    skipped_not_hg += 1
                    continue
                if not match.matched or not match.matched_business_id:
                    skipped_no_match += 1
                    continue

                business = next((b for b in businesses if b.id == match.matched_business_id), None)
                if not business:
                    skipped_no_match += 1
                    continue

                rep = session.scalar(select(Reply).where(Reply.haro_request_id == req.id))
                if rep and rep.send_status == "SENT":
                    skipped_sent += 1
                    continue

                cls = session.scalar(select(Classification).where(Classification.haro_request_id == req.id))
                if not cls:
                    cls = Classification(
                        haro_request_id=req.id,
                        matched=match.matched,
                        matched_business_id=match.matched_business_id,
                        confidence=match.confidence,
                        reasoning_short=match.reasoning_short,
                        topic_tags=json.dumps(match.topic_tags),
                        per_business_audit_json=json.dumps(getattr(match, "per_business_audit", None) or []),
                    )
                    session.add(cls)
                    session.flush()
                else:
                    cls.matched = match.matched
                    cls.matched_business_id = match.matched_business_id
                    cls.confidence = match.confidence
                    cls.reasoning_short = match.reasoning_short
                    cls.topic_tags = json.dumps(match.topic_tags)
                    cls.per_business_audit_json = json.dumps(getattr(match, "per_business_audit", None) or [])

                draft_pair = draft_reply(req, business)
                if draft_pair is None:
                    print("  Skipped request id=%s (Regency draft safety net)" % req.id)
                    continue
                subject, body = draft_pair
                if rep:
                    rep.business_id = business.id
                    rep.reply_subject = subject
                    rep.reply_body = body
                    rep.send_status = "DRAFT"
                    rep.error_message = None
                else:
                    session.add(
                        Reply(
                            haro_request_id=req.id,
                            business_id=business.id,
                            reply_subject=subject,
                            reply_body=body,
                            send_status="DRAFT",
                        )
                    )
                drafted += 1
                print("  Drafted request id=%s -> business id=%s (%s)" % (req.id, business.id, business.name))
            except Exception as exc:
                errors += 1
                print("  ERROR request id=%s: %s" % (req.id, exc))

    print(
        "Done. drafted=%s skipped_not_home_garden=%s skipped_no_business_match=%s skipped_already_sent=%s errors=%s"
        % (drafted, skipped_not_hg, skipped_no_match, skipped_sent, errors)
    )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
