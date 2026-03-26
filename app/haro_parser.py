import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from app.config import settings

LOGGER = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ExtractedRequest:
    category: str
    outlet: str | None
    journalist_name: str | None
    reply_to_email: str | None
    deadline: str | None
    request_text: str
    requirements: dict[str, Any]


def normalize_text(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower())
    return cleaned


def normalize_reply_email_for_dedup(reply_to_email: str | None) -> str:
    """Single source of truth for comparing reply / HARO request-ID addresses."""
    return (reply_to_email or "").strip().lower()


def build_haro_query_id(
    reply_to_email: str | None,
    *,
    inbound_email_id: int,
    slot_index: int,
) -> str:
    """Stable id: **only** the reply-to / request-ID email. No body, outlet, or deadline.

    Same address → same id → one HaroRequest row. Items without a reply address get a
    per-slot key so the unique constraint can still hold (they do not merge).
    """
    email = normalize_reply_email_for_dedup(reply_to_email)
    if email:
        return hashlib.sha256(("reply:%s" % email).encode("utf-8")).hexdigest()
    return hashlib.sha256(
        ("no_reply_to_email:%s:%s" % (inbound_email_id, slot_index)).encode("utf-8")
    ).hexdigest()


def parse_haro_email(body_text: str) -> list[ExtractedRequest]:
    parsed = _parse_with_openai(body_text)
    if parsed:
        return parsed
    LOGGER.warning("OpenAI parsing failed or empty; using fallback parser")
    return _fallback_regex_parse(body_text)


def _parse_with_openai(body_text: str) -> list[ExtractedRequest]:
    if not settings.openai_api_key:
        return []
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        prompt = (
            "Extract all individual HARO journalist requests from the email. "
            "Return strictly valid JSON array where each item has: "
            "category, outlet, journalist_name, reply_to_email, deadline, request_text, requirements. "
            "requirements should be an object. Include null for unknown values. "
            "Do not merge requests."
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "You extract structured HARO request data."},
                {"role": "user", "content": prompt + "\n\nEMAIL:\n" + body_text[:12000]},
            ],
        )
        raw = response.choices[0].message.content or "[]"
        data = json.loads(raw)
        return _coerce_requests(data)
    except Exception as exc:
        LOGGER.exception("OpenAI parse failed: %s", exc)
        return []


def _fallback_regex_parse(body_text: str) -> list[ExtractedRequest]:
    chunks = re.split(r"(?m)^-{5,}\s*$", body_text)
    if len(chunks) <= 1:
        chunks = re.split(r"(?im)\n(?=category:\s*)", body_text)
    chunks = [c.strip() for c in chunks if len(c.strip()) > 120]
    results: list[ExtractedRequest] = []
    for chunk in chunks:
        category = _search_field(chunk, r"(?im)^Category:\s*(.+)$")
        outlet = _search_field(chunk, r"(?im)^Media Outlet:\s*(.+)$") or _search_field(chunk, r"(?im)^Outlet:\s*(.+)$")
        journalist = _search_field(chunk, r"(?im)^Name:\s*(.+)$")
        deadline = _search_field(chunk, r"(?im)^Deadline:\s*(.+)$")
        email_match = EMAIL_RE.search(chunk)
        reply_to_email = email_match.group(0) if email_match else None
        text = chunk.strip()
        requirements = _extract_requirements(chunk)
        if len(text) < 80:
            continue
        results.append(
            ExtractedRequest(
                category=category or "General",
                outlet=outlet,
                journalist_name=journalist,
                reply_to_email=reply_to_email,
                deadline=deadline,
                request_text=text,
                requirements=requirements,
            )
        )
    return results


def _extract_requirements(chunk: str) -> dict[str, Any]:
    req: dict[str, Any] = {}
    words = _search_field(chunk, r"(?im)word count[:\s]+([0-9\- ]+)")
    if words:
        req["word_count"] = words
    expert = _search_field(chunk, r"(?im)(?:looking for|seeking)\s+(.+)")
    if expert:
        req["expertise_requested"] = expert[:200]
    return req


def _search_field(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    if not m:
        return None
    return m.group(1).strip()


def _coerce_requests(data: Any) -> list[ExtractedRequest]:
    results: list[ExtractedRequest] = []
    if not isinstance(data, list):
        return results
    for item in data:
        if not isinstance(item, dict):
            continue
        request_text = (item.get("request_text") or "").strip()
        if len(request_text) < 40:
            continue
        requirements = item.get("requirements")
        if not isinstance(requirements, dict):
            requirements = {}
        results.append(
            ExtractedRequest(
                category=(item.get("category") or "General").strip(),
                outlet=(item.get("outlet") or None),
                journalist_name=(item.get("journalist_name") or None),
                reply_to_email=(item.get("reply_to_email") or None),
                deadline=(item.get("deadline") or None),
                request_text=request_text,
                requirements=requirements,
            )
        )
    return results
