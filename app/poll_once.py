import json
import logging
import threading

from sqlalchemy import select

from app.classifier import MatchResult, classify_request
from app.config import settings
from app.db import get_session, init_db
from app.drafter import draft_reply
from app.regency_niche_gate import is_regency_business
from app.haro_parser import (
    build_haro_query_id,
    normalize_reply_email_for_dedup,
    parse_haro_email,
)
from app.imap_worker import poll_mailboxes
from app.models import (
    AppSetting,
    Business,
    Classification,
    HaroRequest,
    InboundEmail,
    Mailbox,
    Reply,
)
from app.smtp_sender import resolve_destination, send_reply, smtp_mailbox_for_reply
from app.utils import now_utc, setup_logging

LOGGER = logging.getLogger(__name__)

REGENCY_DRAFT_SAFETY_FAIL_REASON = "Regency niche draft safety net rejected generated text."


def _duplicate_sent_same_reply_email(
    session, req: HaroRequest, business_id: int
) -> int | None:
    """If another request has the same reply-to address and already SENT for this business, return that row id."""
    addr = normalize_reply_email_for_dedup(req.reply_to_email)
    if not addr:
        return None
    others = session.scalars(select(HaroRequest).where(HaroRequest.id != req.id)).all()
    for o in others:
        if normalize_reply_email_for_dedup(o.reply_to_email) != addr:
            continue
        rep = session.scalar(select(Reply).where(Reply.haro_request_id == o.id))
        if rep and rep.send_status == "SENT" and rep.business_id == business_id:
            return o.id
    return None


def _finalize_reply_after_draft(
    session,
    reply: Reply,
    req: HaroRequest,
    business: Business,
    inbound: InboundEmail,
    match: MatchResult,
    *,
    global_auto_send: bool,
    global_dry_run: bool,
    global_review_mode: bool,
    sent_count: int,
    max_sends: int,
) -> int:
    """
    Resolve destination, then either SMTP-send (sets SENT + sent_at) or leave DRAFT/SKIPPED with reason.
    Home & garden digest matches are normally kept as drafts for manual send; Regency Shop bypasses that
    when auto-send conditions are met so matched Regency replies can go out automatically.
    Returns updated sent_count.
    """
    destination = resolve_destination(req.reply_to_email, inbound, req.request_text)
    if not destination:
        reply.error_message = "No destination email found."
        return sent_count

    hg_manual_only = bool(match.topic_tags and "home_garden" in match.topic_tags)
    hg_blocks_auto_send = hg_manual_only and not is_regency_business(business)

    should_send = (
        global_auto_send
        and not global_dry_run
        and not global_review_mode
        and business.auto_send_enabled
        and match.confidence >= business.auto_send_threshold
        and sent_count < max_sends
        and not hg_blocks_auto_send
    )
    if should_send:
        dup_of = _duplicate_sent_same_reply_email(session, req, business.id)
        if dup_of is not None:
            reply.send_status = "SKIPPED"
            reply.error_message = "Duplicate reply-to (already sent on request #%s)" % dup_of
            LOGGER.warning(
                "Skip duplicate send request_id=%s same_reply_to_as=%s",
                req.id,
                dup_of,
            )
            return sent_count
        smtp_mb = smtp_mailbox_for_reply(session, business, inbound)
        ok, _msg = send_reply(reply, destination, business, inbound, smtp_mailbox=smtp_mb)
        if ok:
            return sent_count + 1
        return sent_count
    if hg_manual_only:
        reply.send_status = "DRAFT"
        reply.error_message = None
    elif global_dry_run:
        reply.send_status = "SKIPPED"
        reply.error_message = "DRY_RUN enabled"
    elif global_review_mode:
        reply.send_status = "DRAFT"
    else:
        reply.send_status = "SKIPPED"
        reply.error_message = "Auto-send conditions not met"
    return sent_count


# Only one fetch/process cycle at a time (startup, scheduler, after mailbox save).
_poll_execution_lock = threading.Lock()


def run_poll_and_process() -> None:
    """One full poll cycle: fetch from the HARO mailbox, then process pending digests. Safe to call from a background thread."""
    from app.poll_state import (
        notify_skipped,
        poll_begin,
        poll_fetching,
        poll_finish_err,
        poll_finish_ok,
        poll_processing,
    )

    if not _poll_execution_lock.acquire(blocking=False):
        notify_skipped()
        LOGGER.info("run_poll_and_process skipped (already running)")
        return

    poll_begin()
    try:
        try:
            with get_session() as session:
                _seed_defaults(session)
                poll_fetching()
                saved = poll_mailboxes(session)
                LOGGER.info("poll_saved=%s", saved)
                poll_processing()
                processed = process_pending_haro(session)
                LOGGER.info("processed_requests=%s", processed)
            poll_finish_ok(saved, processed)
        except Exception as exc:
            LOGGER.exception("run_poll_and_process failed")
            poll_finish_err(str(exc))
    finally:
        _poll_execution_lock.release()


def main() -> None:
    setup_logging()
    init_db()
    run_poll_and_process()
    # Reprocess only when user clicks "Reprocess requests" (avoids holding DB during startup)


def _looks_like_haro(inbound: InboundEmail) -> bool:
    """True if email subject or body suggests it's a HARO digest (so we parse it)."""
    combined = (" %s %s " % (inbound.subject or "", (inbound.body_text or "")[:10000])).lower()
    return "haro" in combined or "help a reporter out" in combined or "helpareporter" in combined


def process_pending_haro(session) -> int:
    # Process any unprocessed email that is HARO or looks like HARO (subject/body)
    all_pending = session.scalars(
        select(InboundEmail).where(InboundEmail.processed_at.is_(None))
    ).all()
    inbound_rows = []
    for inbound in all_pending:
        if inbound.source == "haro" or _looks_like_haro(inbound):
            if inbound.source != "haro":
                inbound.source = "haro"
            inbound_rows.append(inbound)
    businesses = session.scalars(select(Business).where(Business.enabled.is_(True))).all()
    sent_count = 0
    processed = 0
    max_sends = _setting_int(session, "MAX_SENDS_PER_RUN", settings.max_sends_per_run)
    global_auto_send = _setting_bool(session, "GLOBAL_AUTO_SEND", settings.global_auto_send)
    global_dry_run = _setting_bool(session, "GLOBAL_DRY_RUN", settings.global_dry_run)
    global_review_mode = _setting_bool(session, "GLOBAL_REVIEW_MODE", settings.global_review_mode)

    for inbound in inbound_rows:
        extracted = parse_haro_email(inbound.body_text)
        if not extracted:
            LOGGER.warning(
                "No requests parsed from inbound id=%s subject=%r (email still marked processed). "
                "Check body format or OpenAI/fallback parser.",
                inbound.id,
                (inbound.subject or "")[:120],
            )
        for slot_index, item in enumerate(extracted):
            query_id = build_haro_query_id(
                item.reply_to_email,
                inbound_email_id=inbound.id,
                slot_index=slot_index,
            )
            existing = session.scalar(select(HaroRequest).where(HaroRequest.haro_query_id == query_id))
            if existing:
                LOGGER.debug(
                    "Skipping duplicate query (same haro_query_id) existing request id=%s",
                    existing.id,
                )
                continue

            req = HaroRequest(
                inbound_email_id=inbound.id,
                haro_query_id=query_id,
                category=item.category,
                outlet=item.outlet,
                journalist_name=item.journalist_name,
                reply_to_email=item.reply_to_email,
                deadline=item.deadline,
                request_text=item.request_text,
                requirements_json=json.dumps(item.requirements or {}),
            )
            session.add(req)
            session.flush()

            match = classify_request(req, businesses, inbound_source=inbound.source)
            cls = Classification(
                haro_request_id=req.id,
                matched=match.matched,
                matched_business_id=match.matched_business_id,
                confidence=match.confidence,
                reasoning_short=match.reasoning_short,
                topic_tags=json.dumps(match.topic_tags),
            )
            session.add(cls)
            session.flush()

            if not match.matched or not match.matched_business_id:
                session.add(
                    Reply(
                        haro_request_id=req.id,
                        business_id=None,
                        send_status="SKIPPED",
                        reply_subject="No match",
                        reply_body=match.reasoning_short,
                        error_message="No business match",
                    )
                )
                processed += 1
                continue

            business = next((b for b in businesses if b.id == match.matched_business_id), None)
            if not business:
                processed += 1
                continue
            draft_pair = draft_reply(req, business)
            if draft_pair is None:
                cls.matched = False
                cls.matched_business_id = None
                cls.confidence = 0.0
                cls.reasoning_short = REGENCY_DRAFT_SAFETY_FAIL_REASON
                cls.topic_tags = json.dumps([])
                session.add(
                    Reply(
                        haro_request_id=req.id,
                        business_id=None,
                        send_status="SKIPPED",
                        reply_subject="No match",
                        reply_body=REGENCY_DRAFT_SAFETY_FAIL_REASON,
                        error_message="Regency draft safety net",
                    )
                )
                processed += 1
                continue
            subject, body = draft_pair
            reply = Reply(
                haro_request_id=req.id,
                business_id=business.id,
                reply_subject=subject,
                reply_body=body,
                send_status="DRAFT",
            )
            session.add(reply)
            session.flush()

            if is_regency_business(business):
                LOGGER.info(
                    "regency_niche_audit request_id=%s source=%s action=MATCHED_AND_DRAFTED reply_id=%s",
                    req.id,
                    (inbound.source or "HARO").upper(),
                    reply.id,
                )

            sent_count = _finalize_reply_after_draft(
                session,
                reply,
                req,
                business,
                inbound,
                match,
                global_auto_send=global_auto_send,
                global_dry_run=global_dry_run,
                global_review_mode=global_review_mode,
                sent_count=sent_count,
                max_sends=max_sends,
            )
            processed += 1

        inbound.processed_at = now_utc()
        inbound.status = "PROCESSED"
    return processed


def reprocess_existing_requests(session, progress_callback=None):
    """Re-classify all existing HARO requests against current businesses; create/update drafts where now matched.
    Regenerates draft bodies for every non-SENT match (new classifier + drafter). No-match rows become SKIPPED.
    If progress_callback is given, call it as progress_callback(current, total, message) each step."""
    businesses = session.scalars(select(Business).where(Business.enabled.is_(True))).all()
    if not businesses:
        if progress_callback:
            progress_callback(0, 0, "No enabled businesses")
        return 0
    all_requests = session.scalars(select(HaroRequest).order_by(HaroRequest.id)).all()
    total = len(all_requests)
    if progress_callback:
        progress_callback(0, total, "Starting… %s request(s)" % total)
    max_sends = _setting_int(session, "MAX_SENDS_PER_RUN", settings.max_sends_per_run)
    global_auto_send = _setting_bool(session, "GLOBAL_AUTO_SEND", settings.global_auto_send)
    global_dry_run = _setting_bool(session, "GLOBAL_DRY_RUN", settings.global_dry_run)
    global_review_mode = _setting_bool(session, "GLOBAL_REVIEW_MODE", settings.global_review_mode)
    reprocess_sent = 0
    updated = 0
    for i, req in enumerate(all_requests):
        if progress_callback:
            # Update progress *before* slow work so UI moves immediately
            progress_callback(i + 1, total, "Request %s of %s" % (i + 1, total))
        inbound_row = session.scalar(select(InboundEmail).where(InboundEmail.id == req.inbound_email_id))
        match = classify_request(
            req, businesses, inbound_source=inbound_row.source if inbound_row else None
        )
        cls = session.scalar(select(Classification).where(Classification.haro_request_id == req.id))
        if not cls:
            cls = Classification(
                haro_request_id=req.id,
                matched=match.matched,
                matched_business_id=match.matched_business_id,
                confidence=match.confidence,
                reasoning_short=match.reasoning_short,
                topic_tags=json.dumps(match.topic_tags),
            )
            session.add(cls)
            session.flush()
        else:
            cls.matched = match.matched
            cls.matched_business_id = match.matched_business_id
            cls.confidence = match.confidence
            cls.reasoning_short = match.reasoning_short
            cls.topic_tags = json.dumps(match.topic_tags)

        reply = session.scalar(select(Reply).where(Reply.haro_request_id == req.id))

        if not match.matched or not match.matched_business_id:
            if reply and reply.send_status == "SENT":
                continue
            reason = match.reasoning_short or "No business match"
            if reply:
                reply.business_id = None
                reply.reply_subject = "No match"
                reply.reply_body = reason
                reply.send_status = "SKIPPED"
                reply.error_message = "No business match"
            else:
                session.add(
                    Reply(
                        haro_request_id=req.id,
                        business_id=None,
                        send_status="SKIPPED",
                        reply_subject="No match",
                        reply_body=reason,
                        error_message="No business match",
                    )
                )
            continue

        business = next((b for b in businesses if b.id == match.matched_business_id), None)
        if not business:
            continue
        if reply and reply.send_status == "SENT":
            continue
        draft_pair = draft_reply(req, business)
        if draft_pair is None:
            cls.matched = False
            cls.matched_business_id = None
            cls.confidence = 0.0
            cls.reasoning_short = REGENCY_DRAFT_SAFETY_FAIL_REASON
            cls.topic_tags = json.dumps([])
            if reply:
                reply.business_id = None
                reply.reply_subject = "No match"
                reply.reply_body = REGENCY_DRAFT_SAFETY_FAIL_REASON
                reply.send_status = "SKIPPED"
                reply.error_message = "Regency draft safety net"
            else:
                session.add(
                    Reply(
                        haro_request_id=req.id,
                        business_id=None,
                        send_status="SKIPPED",
                        reply_subject="No match",
                        reply_body=REGENCY_DRAFT_SAFETY_FAIL_REASON,
                        error_message="Regency draft safety net",
                    )
                )
            continue
        subject, body = draft_pair
        if reply:
            reply.business_id = business.id
            reply.reply_subject = subject
            reply.reply_body = body
            reply.send_status = "DRAFT"
            reply.error_message = None
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
        session.flush()
        reply = session.scalar(select(Reply).where(Reply.haro_request_id == req.id))
        if reply and is_regency_business(business):
            LOGGER.info(
                "regency_niche_audit request_id=%s source=%s action=MATCHED_AND_DRAFTED reply_id=%s",
                req.id,
                ((inbound_row.source if inbound_row else None) or "HARO").upper(),
                reply.id,
            )
        if reply:
            reprocess_sent = _finalize_reply_after_draft(
                session,
                reply,
                req,
                business,
                inbound_row,
                match,
                global_auto_send=global_auto_send,
                global_dry_run=global_dry_run,
                global_review_mode=global_review_mode,
                sent_count=reprocess_sent,
                max_sends=max_sends,
            )
        updated += 1
    return updated


def _seed_defaults(session) -> None:
    defaults = {
        "GLOBAL_AUTO_SEND": "true" if settings.global_auto_send else "false",
        "GLOBAL_DRY_RUN": "true" if settings.global_dry_run else "false",
        "GLOBAL_REVIEW_MODE": "true" if settings.global_review_mode else "false",
        "MAX_SENDS_PER_RUN": str(settings.max_sends_per_run),
    }
    for key, value in defaults.items():
        row = session.scalar(select(AppSetting).where(AppSetting.key == key))
        if not row:
            session.add(AppSetting(key=key, value=value))


def _setting_bool(session, key: str, default: bool) -> bool:
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if not row:
        return default
    return row.value.strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(session, key: str, default: int) -> int:
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if not row:
        return default
    try:
        return int(row.value)
    except ValueError:
        return default


if __name__ == "__main__":
    main()
