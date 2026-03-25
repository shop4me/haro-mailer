import json
import logging
import os
import threading
from functools import wraps

from dotenv import load_dotenv
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import desc, func, select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.db import get_session, get_session_for_reprocess
from app.imap_worker import get_haro_mailbox, test_imap_settings
from app.models import AppSetting, Business, Classification, HaroRequest, InboundEmail, Mailbox, Reply
from app.poll_once import reprocess_existing_requests, run_poll_and_process
from app.regency_ai_relevance import (
    REGENCY_RELEVANCE_JSON_SCHEMA,
    REGENCY_RELEVANCE_SYSTEM_PROMPT,
)
from app.smtp_sender import resolve_destination, send_reply, smtp_mailbox_for_reply, test_smtp_settings

bp = Blueprint("main", __name__)
_logger = logging.getLogger(__name__)


def _schedule_immediate_haro_poll() -> None:
    """After saving a mailbox, run one IMAP fetch + process so new HARO settings take effect without waiting for the scheduler."""

    def _run() -> None:
        try:
            run_poll_and_process()
        except Exception:
            _logger.exception("Immediate HARO poll after mailbox save failed")

    threading.Thread(target=_run, daemon=True).start()


# Reprocess progress (shared across pages); access under _reprocess_lock
_reprocess_lock = threading.Lock()
_reprocess_state = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current": 0,
    "message": "",
    "result": None,
    "error": None,
}


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_auth"):
            return redirect(url_for("main.login"))
        return fn(*args, **kwargs)

    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        # Re-read .env so password changes take effect without restart
        load_dotenv()
        expected = os.getenv("ADMIN_PASSWORD") or settings.admin_password
        if pw == expected:
            session["admin_auth"] = True
            return redirect(url_for("main.index"))
        flash("Invalid password", "error")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


def _run_reprocess_background(app):
    # Update immediately so UI shows we started (no app context needed for this)
    with _reprocess_lock:
        _reprocess_state["message"] = "Preparing..."
    with app.app_context():
        def progress_cb(current, total, message):
            with _reprocess_lock:
                _reprocess_state["current"] = current
                _reprocess_state["total"] = total
                _reprocess_state["progress"] = int(round(100 * current / total)) if total else 0
                _reprocess_state["message"] = message or ""

        try:
            with get_session_for_reprocess() as db:
                n = reprocess_existing_requests(db, progress_callback=progress_cb)
            with _reprocess_lock:
                _reprocess_state["running"] = False
                _reprocess_state["progress"] = 100
                _reprocess_state["message"] = "Finished"
                _reprocess_state["result"] = n
                _reprocess_state["error"] = None
        except Exception as e:
            with _reprocess_lock:
                _reprocess_state["running"] = False
                _reprocess_state["error"] = str(e)
                _reprocess_state["result"] = None


@bp.route("/reprocess", methods=["POST"])
@login_required
def reprocess():
    """Start reprocess in background; return JSON so client can poll status."""
    with _reprocess_lock:
        if _reprocess_state["running"]:
            return jsonify({"started": False, "already_running": True})
        _reprocess_state["running"] = True
        _reprocess_state["progress"] = 0
        _reprocess_state["total"] = 0
        _reprocess_state["current"] = 0
        _reprocess_state["message"] = "Starting..."
        _reprocess_state["result"] = None
        _reprocess_state["error"] = None
    app_obj = current_app._get_current_object()
    t = threading.Thread(target=_run_reprocess_background, args=(app_obj,), daemon=True)
    t.start()
    return jsonify({"started": True})


@bp.route("/reprocess/status")
@login_required
def reprocess_status():
    """Return current reprocess progress (for polling from any page)."""
    with _reprocess_lock:
        out = dict(_reprocess_state)
    return jsonify(out)


@bp.route("/poll/status")
@login_required
def poll_status():
    """IMAP fetch + HARO processing progress for the Inbound page."""
    from app.poll_state import snapshot

    out = snapshot()
    out["lookback_hours"] = settings.lookback_hours
    out["poll_interval_minutes"] = settings.poll_interval_minutes
    return jsonify(out)


@bp.route("/")
@login_required
def index():
    with get_session() as db:
        haro_mailbox = get_haro_mailbox(db)
        inbound_count = db.scalar(select(func.count()).select_from(InboundEmail)) or 0
        request_count = db.scalar(select(func.count()).select_from(HaroRequest)) or 0
        matched_count = db.scalar(
            select(func.count()).select_from(Classification).where(Classification.matched.is_(True))
        ) or 0
        sent_count = db.scalar(select(func.count()).select_from(Reply).where(Reply.send_status == "SENT")) or 0
        draft_count = db.scalar(select(func.count()).select_from(Reply).where(Reply.send_status == "DRAFT")) or 0
    return render_template(
        "index.html",
        haro_mailbox=haro_mailbox,
        inbound_count=inbound_count,
        request_count=request_count,
        matched_count=matched_count,
        sent_count=sent_count,
        draft_count=draft_count,
    )


@bp.route("/inbound-emails")
@login_required
def inbound_emails():
    with get_session() as db:
        # Show everything fetched from the HARO mailbox (not only source=haro — classification can miss HTML-heavy digests)
        mb = get_haro_mailbox(db)
        if mb:
            rows = db.scalars(
                select(InboundEmail)
                .where(InboundEmail.mailbox_id == mb.id)
                .order_by(desc(InboundEmail.received_at))
                .limit(200)
            ).all()
        else:
            rows = db.scalars(
                select(InboundEmail)
                .where(InboundEmail.source == "haro")
                .order_by(desc(InboundEmail.received_at))
                .limit(200)
            ).all()
        # Total parsed requests per email (not only matched — so inbound reflects real parse results)
        count_q = (
            select(HaroRequest.inbound_email_id, func.count(HaroRequest.id).label("n"))
            .select_from(HaroRequest)
            .group_by(HaroRequest.inbound_email_id)
        )
        found_counts = {r.inbound_email_id: r.n for r in db.execute(count_q).all()}
    return render_template(
        "inbound_emails.html",
        rows=rows,
        found_counts=found_counts,
        settings=settings,
    )


@bp.route("/inbound-emails/<int:inbound_id>/found")
@login_required
def inbound_found(inbound_id: int):
    with get_session() as db:
        inbound = db.scalar(select(InboundEmail).where(InboundEmail.id == inbound_id))
        if not inbound:
            flash("Email not found", "error")
            return redirect(url_for("main.inbound_emails"))
        # All parsed requests from this email (matched and not matched), with classification and reply
        reqs = db.scalars(
            select(HaroRequest)
            .where(HaroRequest.inbound_email_id == inbound_id)
            .order_by(HaroRequest.id)
        ).all()
        result = []
        for req in reqs:
            cls = db.scalar(select(Classification).where(Classification.haro_request_id == req.id))
            biz = None
            if cls and cls.matched_business_id:
                biz = db.scalar(select(Business).where(Business.id == cls.matched_business_id))
            rep = db.scalar(select(Reply).where(Reply.haro_request_id == req.id))
            result.append((req, cls, biz, rep))
        # Relevant or replied first: matched to a business, or has a reply (sent/draft)
        rows_relevant = [
            r for r in result
            if (r[1] and r[1].matched and r[2]) or (r[3] and r[3].send_status in ("SENT", "DRAFT"))
        ]
        # Sort relevant: SENT first, then DRAFT, then matched-only (by request id)
        def _relevant_order(r):
            req, cls, biz, rep = r
            if rep and rep.send_status == "SENT":
                return (0, req.id)
            if rep and rep.send_status == "DRAFT":
                return (1, req.id)
            return (2, req.id)
        rows_relevant.sort(key=_relevant_order)
        rows_other = [r for r in result if r not in rows_relevant]
    return render_template(
        "inbound_found.html",
        inbound=inbound,
        rows_relevant=rows_relevant,
        rows_other=rows_other,
    )


@bp.route("/haro-requests")
@login_required
def haro_requests():
    business_id = request.args.get("business_id", type=int)
    min_conf = request.args.get("min_conf", type=float)
    status = request.args.get("status", "")
    with get_session() as db:
        businesses = db.scalars(select(Business).where(Business.enabled.is_(True))).all()
        # Newest parsed requests first (what users expect when checking "latest"). Deadline as tie-breaker.
        rows = db.scalars(
            select(HaroRequest)
            .order_by(desc(HaroRequest.created_at), HaroRequest.deadline.asc().nulls_last())
            .limit(500)
        ).all()
        result = []
        for req in rows:
            cls = db.scalar(select(Classification).where(Classification.haro_request_id == req.id))
            rep = db.scalar(select(Reply).where(Reply.haro_request_id == req.id))
            if business_id and (not cls or cls.matched_business_id != business_id):
                continue
            if min_conf is not None and (not cls or cls.confidence < min_conf):
                continue
            if status and (not rep or rep.send_status != status):
                continue
            result.append((req, cls, rep))
    return render_template("haro_requests.html", rows=result, businesses=businesses)


@bp.route("/haro-requests/<int:request_id>", methods=["GET", "POST"])
@login_required
def haro_request_detail(request_id: int):
    with get_session() as db:
        req = db.scalar(select(HaroRequest).where(HaroRequest.id == request_id))
        if not req:
            flash("Request not found", "error")
            return redirect(url_for("main.haro_requests"))
        cls = db.scalar(select(Classification).where(Classification.haro_request_id == req.id))
        rep = db.scalar(select(Reply).where(Reply.haro_request_id == req.id))
        inbound = db.scalar(select(InboundEmail).where(InboundEmail.id == req.inbound_email_id))
        biz = db.scalar(select(Business).where(Business.id == rep.business_id)) if rep and rep.business_id else None

        if request.method == "POST" and rep and biz:
            rep.reply_subject = request.form.get("reply_subject", rep.reply_subject)
            rep.reply_body = request.form.get("reply_body", rep.reply_body)
            action = request.form.get("action")
            if action == "send":
                destination = resolve_destination(req.reply_to_email, inbound, req.request_text) if inbound else None
                if not destination:
                    flash("No destination email found", "error")
                else:
                    smtp_mb = smtp_mailbox_for_reply(db, biz, inbound)
                    ok, msg = send_reply(rep, destination, biz, inbound, smtp_mailbox=smtp_mb)
                    flash("Sent" if ok else f"Failed: {msg}", "info")
            elif action == "skip":
                rep.send_status = "SKIPPED"
                rep.error_message = "Skipped by admin"
                flash("Marked as skipped", "info")
        return render_template("haro_request_detail.html", req=req, cls=cls, rep=rep, inbound=inbound, biz=biz)


@bp.route("/replies")
@login_required
def replies():
    with get_session() as db:
        rows = (
            db.scalars(
                select(Reply)
                .options(joinedload(Reply.business), joinedload(Reply.haro_request))
                .order_by(desc(Reply.id))
                .limit(500)
            )
            .all()
        )
    return render_template("replies.html", rows=rows)


@bp.route("/replies/<int:reply_id>/send", methods=["POST"])
@login_required
def reply_send(reply_id: int):
    with get_session() as db:
        rep = db.scalar(select(Reply).where(Reply.id == reply_id))
        if not rep:
            flash("Reply not found.", "error")
            return redirect(url_for("main.replies"))
        if rep.send_status != "DRAFT":
            flash("Only drafts can be sent from here.", "error")
            return redirect(url_for("main.replies"))
        req = db.scalar(select(HaroRequest).where(HaroRequest.id == rep.haro_request_id))
        if not req:
            flash("Linked request is missing.", "error")
            return redirect(url_for("main.replies"))
        biz = db.scalar(select(Business).where(Business.id == rep.business_id)) if rep.business_id else None
        if not biz:
            flash("No business linked to this reply.", "error")
            return redirect(url_for("main.replies"))
        inbound = db.scalar(select(InboundEmail).where(InboundEmail.id == req.inbound_email_id))
        destination = resolve_destination(req.reply_to_email, inbound, req.request_text) if inbound else None
        if not destination:
            flash("No destination email found for this request.", "error")
            return redirect(url_for("main.replies"))
        smtp_mb = smtp_mailbox_for_reply(db, biz, inbound)
        ok, msg = send_reply(rep, destination, biz, inbound, smtp_mailbox=smtp_mb)
        flash("Sent." if ok else ("Send failed: %s" % msg), "info" if ok else "error")
    return redirect(url_for("main.replies"))


def _form_int(key: str, default: int | None = None) -> int | None:
    raw = request.form.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _form_float(key: str, default: float) -> float:
    raw = request.form.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@bp.route("/businesses", methods=["GET", "POST"])
@login_required
def businesses():
    if request.method == "POST":
        try:
            with get_session() as db:
                b_id = _form_int("id")
                b = db.scalar(select(Business).where(Business.id == b_id)) if b_id else Business()
                if not b_id:
                    db.add(b)
                b.name = request.form.get("name", "").strip() or "Unnamed"
                b.contact_name = request.form.get("contact_name", "")
                b.nature_of_business = request.form.get("nature_of_business", "")
                b.keywords = request.form.get("keywords", "")
                b.brand_voice = request.form.get("brand_voice", "")
                b.website_url = request.form.get("website_url", "")
                b.signature = request.form.get("signature", "")
                b.enabled = request.form.get("enabled") in ("on", "true", "1", "yes")
                b.auto_send_enabled = request.form.get("auto_send_enabled") in ("on", "true", "1", "yes")
                b.auto_send_threshold = _form_float("auto_send_threshold", 0.8)
                mb_raw = (request.form.get("mailbox_id") or "").strip()
                if not mb_raw:
                    raise ValueError("Select a mailbox. Outgoing mail is sent only through mailboxes.")
                try:
                    mb_id = int(mb_raw)
                except ValueError as e:
                    raise ValueError("Invalid mailbox.") from e
                mb_row = db.scalar(select(Mailbox).where(Mailbox.id == mb_id))
                if not mb_row:
                    raise ValueError("Mailbox not found.")
                b.mailbox_id = mb_id
                b.sending_email = (mb_row.imap_user or mb_row.smtp_user or "").strip()
                b.smtp_host = ""
                b.smtp_port = 587
                b.smtp_user = ""
                b.smtp_password = ""
                b.strict_ai_relevance_enabled = request.form.get("strict_ai_relevance_enabled") in (
                    "on",
                    "true",
                    "1",
                    "yes",
                )
                b.strict_ai_relevance_system_prompt = request.form.get(
                    "strict_ai_relevance_system_prompt", ""
                )
                b.strict_ai_relevance_min_confidence = _form_float(
                    "strict_ai_relevance_min_confidence", 0.82
                )
            flash("Business saved.", "info")
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:
            flash("Error saving business: %s" % str(e), "error")
        return redirect(url_for("main.businesses"))
    edit_id = request.args.get("edit", type=int)
    business_edit = None
    pipeline_settings = {}
    try:
        with get_session() as db:
            rows = (
                db.scalars(
                    select(Business)
                    .options(joinedload(Business.mailbox))
                    .order_by(desc(Business.id))
                )
                .all()
            )
            mailboxes = db.scalars(select(Mailbox).order_by(Mailbox.label)).all()
            if edit_id:
                business_edit = db.scalar(
                    select(Business)
                    .options(joinedload(Business.mailbox))
                    .where(Business.id == edit_id)
                )
            keys = ["GLOBAL_AUTO_SEND", "GLOBAL_DRY_RUN", "GLOBAL_REVIEW_MODE", "MAX_SENDS_PER_RUN"]
            pipeline_settings = {
                r.key: r.value
                for r in db.scalars(select(AppSetting).where(AppSetting.key.in_(keys))).all()
            }
    except Exception as e:
        flash("Error loading businesses: %s" % str(e), "error")
        rows = []
        mailboxes = []
    strict_schema_json = json.dumps(REGENCY_RELEVANCE_JSON_SCHEMA, indent=2)
    # New business: pre-fill strict prompt with default template; edit: use stored value.
    if business_edit:
        strict_prompt_field_value = business_edit.strict_ai_relevance_system_prompt or ""
    else:
        strict_prompt_field_value = REGENCY_RELEVANCE_SYSTEM_PROMPT
    return render_template(
        "businesses.html",
        rows=rows,
        business_edit=business_edit,
        mailboxes=mailboxes,
        pipeline_settings=pipeline_settings,
        strict_schema_json=strict_schema_json,
        strict_prompt_field_value=strict_prompt_field_value,
        home_garden_business_id=settings.home_garden_business_id,
    )


@bp.route("/businesses/<int:business_id>/delete", methods=["POST"])
@login_required
def business_delete(business_id: int):
    with get_session() as db:
        biz = db.scalar(select(Business).where(Business.id == business_id))
        if not biz:
            flash("Business not found", "error")
            return redirect(url_for("main.businesses"))
        for rep in db.scalars(select(Reply).where(Reply.business_id == business_id)).all():
            rep.business_id = None
        for cls in db.scalars(select(Classification).where(Classification.matched_business_id == business_id)).all():
            cls.matched_business_id = None
            cls.matched = False
        db.delete(biz)
    flash("Business deleted.", "info")
    return redirect(url_for("main.businesses"))


@bp.route("/businesses/<int:business_id>/autoreply", methods=["POST"])
@login_required
def business_autoreply(business_id: int):
    """Toggle per-business autoreply (maps to Business.auto_send_enabled)."""
    enabled = request.form.get("autoreply") == "1"
    try:
        with get_session() as db:
            biz = db.scalar(select(Business).where(Business.id == business_id))
            if not biz:
                flash("Business not found.", "error")
                return redirect(url_for("main.businesses"))
            name = (biz.name or "").strip() or ("#%s" % business_id)
            biz.auto_send_enabled = enabled
        flash("Autoreply for %s is now %s." % (name, "on" if enabled else "off"), "info")
    except Exception as e:
        flash("Could not update autoreply: %s" % str(e), "error")
    return redirect(url_for("main.businesses"))


@bp.route("/mailboxes/test", methods=["POST"])
@login_required
def mailboxes_test():
    """Test IMAP and (if SMTP host set) SMTP using one email/password from the form. Does not save."""
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict()
    imap_host = (data.get("imap_host") or "").strip()
    user = (data.get("imap_user") or "").strip()
    pw = (data.get("imap_password") or "").strip()
    folder = (data.get("folder") or "INBOX").strip() or "INBOX"
    try:
        imap_port = int(data.get("imap_port") or 993)
    except (TypeError, ValueError):
        imap_port = 993
    skip_raw = data.get("imap_skip_ssl_verify")
    if isinstance(skip_raw, bool):
        skip = skip_raw
    else:
        skip = str(skip_raw or "").lower() in ("true", "1", "on", "yes")

    smtp_host = (data.get("smtp_host") or "").strip()
    try:
        smtp_port = int(data.get("smtp_port") or 587)
    except (TypeError, ValueError):
        smtp_port = 587

    if not imap_host or not user:
        return jsonify({"ok": False, "message": "IMAP host and email are required."}), 400

    m_id = data.get("id") or data.get("mailbox_id")
    if not pw and m_id:
        try:
            mid = int(m_id)
        except (TypeError, ValueError):
            mid = None
        if mid:
            with get_session() as db:
                existing = db.scalar(select(Mailbox).where(Mailbox.id == mid))
                if existing:
                    pw = existing.imap_password or ""

    if not pw:
        return jsonify(
            {
                "ok": False,
                "message": "Enter the password to test, or set ID to reuse the saved password for this mailbox.",
            }
        ), 400

    imap_ok, imap_msg = test_imap_settings(
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=user,
        imap_password=pw,
        folder=folder,
        imap_skip_ssl_verify=skip,
    )
    parts = ["Incoming mail: %s" % ("OK" if imap_ok else imap_msg)]
    smtp_ok = None
    if smtp_host:
        smtp_ok, smtp_msg = test_smtp_settings(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=user,
            smtp_password=pw,
        )
        parts.append("Outgoing mail: %s" % ("OK" if smtp_ok else smtp_msg))
    else:
        parts.append("Outgoing mail: skipped (not configured).")

    overall = imap_ok and (smtp_ok is None or smtp_ok is True)
    return jsonify({"ok": overall, "message": " ".join(parts), "imap_ok": imap_ok, "smtp_ok": smtp_ok})


def _delete_mailbox_and_related(db, mailbox_id: int) -> bool:
    """Delete a mailbox and all inbound emails (and their requests) that reference it."""
    m = db.scalar(select(Mailbox).where(Mailbox.id == mailbox_id))
    if not m:
        return False
    inbounds = db.scalars(select(InboundEmail).where(InboundEmail.mailbox_id == mailbox_id)).all()
    for inbound in inbounds:
        for hr in list(inbound.requests):
            if hr.classification:
                db.delete(hr.classification)
            if hr.reply:
                db.delete(hr.reply)
            db.delete(hr)
        db.delete(inbound)
    db.delete(m)
    return True


@bp.route("/mailboxes/<int:mailbox_id>/delete", methods=["POST"])
@login_required
def mailbox_delete(mailbox_id: int):
    try:
        with get_session() as db:
            for b in db.scalars(select(Business).where(Business.mailbox_id == mailbox_id)).all():
                b.mailbox_id = None
            if not _delete_mailbox_and_related(db, mailbox_id):
                flash("Mailbox not found.", "error")
                return redirect(url_for("main.mailboxes"))
        flash("Mailbox deleted.", "info")
    except Exception as e:
        flash("Error deleting mailbox: %s" % str(e), "error")
    return redirect(url_for("main.mailboxes"))


@bp.route("/mailboxes", methods=["GET", "POST"])
@login_required
def mailboxes():
    if request.method == "POST":
        try:
            with get_session() as db:
                m_id = _form_int("id")
                m = db.scalar(select(Mailbox).where(Mailbox.id == m_id)) if m_id else Mailbox()
                if not m_id:
                    db.add(m)
                m.label = request.form.get("label", "").strip() or "Unnamed"
                m.imap_host = request.form.get("imap_host", "")
                m.imap_port = _form_int("imap_port") or 993
                m.imap_user = request.form.get("imap_user", "").strip()
                pw = request.form.get("imap_password", "")
                if pw:
                    m.imap_password = pw
                    m.smtp_password = pw
                m.smtp_host = request.form.get("smtp_host", "").strip()
                m.smtp_port = _form_int("smtp_port") or 587
                # Same login for incoming and outgoing
                m.smtp_user = m.imap_user
                m.folder = request.form.get("folder", "INBOX") or "INBOX"
                m.enabled = request.form.get("enabled") in ("on", "true", "1", "yes")
                m.imap_skip_ssl_verify = request.form.get("imap_skip_ssl_verify") in ("on", "true", "1", "yes")
                use_for_haro = request.form.get("use_for_haro") in ("on", "true", "1", "yes")
                if use_for_haro:
                    for other in db.scalars(select(Mailbox)).all():
                        other.use_for_haro = False
                m.use_for_haro = use_for_haro
            flash("Mailbox saved. Fetching mail now…", "info")
            _schedule_immediate_haro_poll()
        except Exception as e:
            flash("Error saving mailbox: %s" % str(e), "error")
        return redirect(url_for("main.mailboxes"))
    edit_id = request.args.get("edit", type=int)
    edit_mailbox = None
    try:
        with get_session() as db:
            rows = db.scalars(select(Mailbox).order_by(desc(Mailbox.id))).all()
            if edit_id:
                edit_mailbox = db.scalar(select(Mailbox).where(Mailbox.id == edit_id))
    except Exception as e:
        flash("Error loading mailboxes: %s" % str(e), "error")
        rows = []
    return render_template("mailboxes.html", rows=rows, edit_mailbox=edit_mailbox)


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def app_settings():
    keys = ["GLOBAL_DRY_RUN", "GLOBAL_REVIEW_MODE", "GLOBAL_AUTO_SEND", "MAX_SENDS_PER_RUN"]
    with get_session() as db:
        if request.method == "POST":
            for key in keys:
                row = db.scalar(select(AppSetting).where(AppSetting.key == key))
                if not row:
                    row = AppSetting(key=key, value="")
                    db.add(row)
                if key.startswith("GLOBAL_"):
                    row.value = "true" if request.form.get(key) else "false"
                else:
                    row.value = request.form.get(key, "20")
            flash("Settings saved", "info")
        rows = {r.key: r.value for r in db.scalars(select(AppSetting).where(AppSetting.key.in_(keys))).all()}
    return render_template("settings.html", rows=rows)
