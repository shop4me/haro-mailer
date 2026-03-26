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


def _normalize_request_for_dedup(request_text: str) -> str:
    """Normalize query body so minor punctuation / model variance does not split duplicates."""
    n = normalize_text(request_text)
    # Drop punctuation that OpenAI extractions often vary on (quotes, dashes, etc.)
    n = re.sub(r"[^\w\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def build_haro_query_id(
    request_text: str,
    outlet: str | None,
    deadline: str | None,
    reply_to_email: str | None = None,
) -> str:
    """Stable id for deduplicating journalist queries across digests.

    **Deadline is intentionally not part of the hash.** HARO often repeats the same
    request with reworded deadlines (\"Tomorrow 5pm\" vs \"Mar 26 5pm ET\"), which
    previously produced different IDs and duplicate replies/sends.

    **reply_to_email** (when present) distinguishes two different journalists; if missing,
    we may fall back to the first email found in *request_text*.

    *deadline* is kept in the signature for callers but is not used in the hash.
    """
    _ = deadline
    contact = normalize_text(reply_to_email or "") or (_extract_first_email(request_text) or "")
    dedup_basis = "||".join(
        [
            _normalize_request_for_dedup(request_text),
            normalize_text(outlet or ""),
            contact,
        ]
    )
    return hashlib.sha256(dedup_basis.encode("utf-8")).hexdigest()


def _extract_first_email(text: str) -> str | None:
    m = EMAIL_RE.search(text or "")
    return m.group(0).lower() if m else None


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
