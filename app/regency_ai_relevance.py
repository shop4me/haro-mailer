"""
AI-only relevance gate for Regency Shop (furniture / home / interiors).
No keyword lists: the model decides if a journalist request is genuinely in-lane.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from openai import OpenAI

from app.config import settings
from app.models import Business, HaroRequest

LOGGER = logging.getLogger(__name__)

# Stricter than the general HARO classifier: editor protecting outbound quality.
REGENCY_RELEVANCE_MIN_CONFIDENCE = 0.82

RegencyDecision = Literal["RELEVANT", "NOT_RELEVANT"]
NicheFit = Literal[
    "furniture",
    "home_decor",
    "interiors",
    "home_office",
    "lighting",
    "fireplace",
    "cabinetry",
    "remodeling",
    "garden_adjacent",
    "none",
]

# Exact schema returned by the model (strict JSON). Used for API + docs.
REGENCY_RELEVANCE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["RELEVANT", "NOT_RELEVANT"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "niche_fit": {
            "type": "string",
            "enum": [
                "furniture",
                "home_decor",
                "interiors",
                "home_office",
                "lighting",
                "fireplace",
                "cabinetry",
                "remodeling",
                "garden_adjacent",
                "none",
            ],
        },
        "reasoning": {"type": "string"},
    },
    "required": ["decision", "confidence", "niche_fit", "reasoning"],
    "additionalProperties": False,
}

REGENCY_RELEVANCE_SYSTEM_PROMPT = """You are a strict relevance classifier for Regency Shop.

Regency Shop is relevant only for:
furniture, home decor, interiors, home office furniture, lighting, fireplaces, cabinetry, remodeling/design related to the home, and closely related home-and-garden topics.

Your job:
Decide whether the journalist request is genuinely relevant to Regency Shop.

Rules:
- Be strict.
- Do not stretch to make a match.
- Do not approve something just because the company could technically comment.
- If the request is outside the niche, return NOT_RELEVANT.
- If the brand would only be able to send a refusal or vague generic comment, return NOT_RELEVANT.
- Only return RELEVANT if the request clearly belongs in the company's true editorial lane.

Return JSON only:
{
  "decision": "RELEVANT" or "NOT_RELEVANT",
  "confidence": 0.0 to 1.0,
  "niche_fit": "furniture" | "home_decor" | "interiors" | "home_office" | "lighting" | "fireplace" | "cabinetry" | "remodeling" | "garden_adjacent" | "none",
  "reasoning": "short explanation"
}"""


def request_summary(request: HaroRequest, max_len: int = 120) -> str:
    t = (request.request_text or "").replace("\n", " ").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _request_context_block(request: HaroRequest) -> str:
    parts = [
        f"Category: {request.category or '(none)'}",
        f"Outlet: {request.outlet or '(none)'}",
        "",
        "Query:",
        (request.request_text or "").strip()[:8000],
    ]
    return "\n".join(parts)


def _normalize_result(data: dict[str, Any]) -> RegencyAiRelevanceResult:
    decision_raw = str(data.get("decision", "")).strip().upper()
    decision: RegencyDecision = "NOT_RELEVANT"
    if decision_raw == "RELEVANT":
        decision = "RELEVANT"
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reasoning = str(data.get("reasoning", "") or "").strip() or "No reasoning provided."
    nf_raw = str(data.get("niche_fit", "none") or "none").strip().lower()
    allowed_nf: set[str] = {
        "furniture",
        "home_decor",
        "interiors",
        "home_office",
        "lighting",
        "fireplace",
        "cabinetry",
        "remodeling",
        "garden_adjacent",
        "none",
    }
    niche_fit: NicheFit = nf_raw if nf_raw in allowed_nf else "none"
    return RegencyAiRelevanceResult(
        decision=decision,
        confidence=conf,
        reasoning=reasoning[:500],
        niche_fit=niche_fit,
    )


@dataclass
class RegencyAiRelevanceResult:
    decision: RegencyDecision
    confidence: float
    reasoning: str
    niche_fit: NicheFit
    error: str | None = None


def _min_conf_for_business(business: Business | None) -> float:
    if business is None:
        return REGENCY_RELEVANCE_MIN_CONFIDENCE
    try:
        v = float(getattr(business, "strict_ai_relevance_min_confidence", REGENCY_RELEVANCE_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        v = REGENCY_RELEVANCE_MIN_CONFIDENCE
    if v <= 0:
        v = REGENCY_RELEVANCE_MIN_CONFIDENCE
    return max(0.0, min(1.0, v))


def allows_regency_drafting(
    result: RegencyAiRelevanceResult,
    min_conf: float | None = None,
) -> bool:
    if result.error:
        return False
    mc = REGENCY_RELEVANCE_MIN_CONFIDENCE if min_conf is None else max(0.0, min(1.0, float(min_conf)))
    return result.decision == "RELEVANT" and result.confidence >= mc


def log_regency_ai_audit(
    request: HaroRequest,
    source_type: str | None,
    result: RegencyAiRelevanceResult,
    drafting_allowed: bool,
) -> None:
    rid = getattr(request, "id", None)
    src = (source_type or "HARO").upper()
    summary = request_summary(request)
    LOGGER.info(
        "regency_ai_relevance_audit request_id=%s source=%s summary=%r decision=%s confidence=%.3f "
        "niche_fit=%s drafting=%s reasoning=%s",
        rid,
        src,
        summary,
        result.decision,
        result.confidence,
        result.niche_fit,
        "allowed" if drafting_allowed else "blocked",
        (result.reasoning[:280] + "…") if len(result.reasoning) > 280 else result.reasoning,
    )
    if result.error:
        LOGGER.warning("regency_ai_relevance_audit request_id=%s classifier_error=%s", rid, result.error)


def classify_regency_relevance(
    request: HaroRequest,
    business: Business | None = None,
) -> RegencyAiRelevanceResult:
    """
    Call the strict AI relevance model for this business (prompt + threshold from DB when provided).
    On missing API key or failure, fail closed (NOT_RELEVANT).
    """
    if not settings.openai_api_key:
        err = "OPENAI_API_KEY not set; Regency relevance classifier cannot run."
        LOGGER.warning(err)
        return RegencyAiRelevanceResult(
            "NOT_RELEVANT",
            0.0,
            err,
            "none",
            error=err,
        )

    brand = (business.name or "Business").strip() if business else "Regency Shop"
    system_prompt = REGENCY_RELEVANCE_SYSTEM_PROMPT
    if business is not None:
        custom = (getattr(business, "strict_ai_relevance_system_prompt", None) or "").strip()
        if custom:
            system_prompt = custom

    user_content = (
        "Classify this HARO or SOS journalist request for %s.\n\n" % brand
        + f"{_request_context_block(request)}\n"
    )

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "regency_relevance",
                    "strict": True,
                    "schema": REGENCY_RELEVANCE_JSON_SCHEMA,
                },
            },
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ValueError("Empty model response")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        return _normalize_result(data)
    except Exception as exc:
        err = str(exc)
        LOGGER.exception("Regency AI relevance classifier failed: %s", exc)
        return RegencyAiRelevanceResult(
            "NOT_RELEVANT",
            0.0,
            "Classifier error; request blocked.",
            "none",
            error=err[:300],
        )


def is_regency_business(business: Business | None) -> bool:
    """True if this business row is Regency Shop (AI relevance gate applies)."""
    if not business:
        return False
    n = (business.name or "").strip().lower()
    if n == "regency shop":
        return True
    return "regency" in n and "shop" in n


def strict_relevance_gate_applies(business: Business | None) -> bool:
    """Run strict AI relevance when Regency by name or explicitly enabled on the business row."""
    if not business:
        return False
    if is_regency_business(business):
        return True
    return bool(getattr(business, "strict_ai_relevance_enabled", False))


def _upsert_strict_audit(
    audit: list[dict],
    biz: Business,
    relevant: bool,
    reason: str,
) -> None:
    bid = biz.id
    name = (biz.name or "").strip()
    for row in audit:
        if row.get("business_id") == bid:
            row["relevant"] = relevant
            row["reason"] = reason[:500]
            row["source"] = "strict_ai_relevance"
            if name:
                row["name"] = name
            return
    audit.append(
        {
            "business_id": bid,
            "name": name or ("Business %s" % bid),
            "relevant": relevant,
            "reason": reason[:500],
            "source": "strict_ai_relevance",
        }
    )


def apply_regency_relevance_gate(
    request: HaroRequest,
    matched: bool,
    matched_business_id: int | None,
    businesses: list[Business],
    inbound_source: str | None,
    reasoning_short: str,
    topic_tags: list[str],
    per_business_audit: list[dict] | None = None,
) -> tuple[bool, int | None, str, list[str], list[dict]]:
    """
    If the matched business uses strict AI relevance (Regency Shop or enabled), run the classifier.
    Returns (matched, business_id, reasoning_short, topic_tags, per_business_audit).
    """
    audit = list(per_business_audit or [])
    if not matched or not matched_business_id:
        return matched, matched_business_id, reasoning_short, topic_tags, audit

    biz = next((b for b in businesses if b.id == matched_business_id), None)
    if not strict_relevance_gate_applies(biz):
        return matched, matched_business_id, reasoning_short, topic_tags, audit

    min_conf = _min_conf_for_business(biz)
    result = classify_regency_relevance(request, biz)
    drafting = allows_regency_drafting(result, min_conf)
    log_regency_ai_audit(request, inbound_source, result, drafting)

    if not drafting:
        if result.error:
            reason = "Strict AI relevance unavailable; no match."
        elif result.decision == "NOT_RELEVANT":
            reason = "Strict AI relevance: NOT_RELEVANT — %s" % result.reasoning[:200]
        else:
            reason = (
                "Strict AI relevance: confidence %.2f below threshold (need >= %.2f). %s"
                % (result.confidence, min_conf, result.reasoning[:120])
            )
        _upsert_strict_audit(audit, biz, False, reason)
        return False, None, reason[:240], [], audit

    tags = list(topic_tags)
    if "strict_ai_relevant" not in tags:
        tags.append("strict_ai_relevant")
    if is_regency_business(biz) and "regency_ai_relevant" not in tags:
        tags.append("regency_ai_relevant")
    if result.niche_fit and result.niche_fit != "none":
        t = "niche_%s" % result.niche_fit
        if t not in tags:
            tags.append(t)

    new_reason = "Strict AI relevance: RELEVANT (%s, conf=%.2f) — %s" % (
        result.niche_fit,
        result.confidence,
        result.reasoning[:120],
    )
    _upsert_strict_audit(audit, biz, True, new_reason)
    return True, matched_business_id, new_reason[:240], tags, audit


# Backward-compatible name for classifier imports
apply_regency_niche_gate_to_match = apply_regency_relevance_gate
