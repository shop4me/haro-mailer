from app.haro_parser import build_haro_query_id, parse_haro_email


def test_query_id_is_only_reply_email_case_insensitive():
    a = build_haro_query_id("Reporter@HelpAReporter.com", inbound_email_id=1, slot_index=0)
    b = build_haro_query_id("  REPORTER@HELPAREPORTER.COM  ", inbound_email_id=99, slot_index=9)
    assert a == b


def test_different_reply_emails_different_ids():
    a = build_haro_query_id("a@x.com", inbound_email_id=1, slot_index=0)
    b = build_haro_query_id("b@y.com", inbound_email_id=1, slot_index=0)
    assert a != b


def test_body_and_slot_ignored_when_reply_email_set():
    """Outlet/body/deadline must not affect id — only reply address."""
    a = build_haro_query_id("same@haro.test", inbound_email_id=1, slot_index=0)
    b = build_haro_query_id("same@haro.test", inbound_email_id=2, slot_index=7)
    assert a == b


def test_no_reply_email_uses_inbound_and_slot():
    a = build_haro_query_id(None, inbound_email_id=5, slot_index=0)
    b = build_haro_query_id("", inbound_email_id=5, slot_index=1)
    assert a != b
    c = build_haro_query_id(None, inbound_email_id=5, slot_index=0)
    assert a == c


def test_fallback_parser_extracts_multiple_requests():
    body = """
Category: Home
Outlet: Style Magazine
Name: Jane Reporter
Deadline: 2026-03-03
Email: jane@example.com
Looking for interior designers to comment on sofa trends for 2026. Include practical tips and examples.
-----
Category: Business
Outlet: Founder Daily
Name: Alex Writer
Deadline: 2026-03-04
Send responses to: alex@founderdaily.com
Seeking small business owners with direct experience improving customer retention and loyalty programs.
"""
    items = parse_haro_email(body)
    assert len(items) >= 2
    assert any(i.reply_to_email and "@" in i.reply_to_email for i in items)
