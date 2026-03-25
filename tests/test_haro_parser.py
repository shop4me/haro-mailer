from app.haro_parser import build_haro_query_id, parse_haro_email


def test_hash_is_idempotent_for_whitespace_and_case():
    a = build_haro_query_id("Need Experts on SOFAS", "Home Weekly", "Tomorrow 5pm ET")
    b = build_haro_query_id(" need experts on sofas  ", "home weekly", " tomorrow 5pm et ")
    assert a == b


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
