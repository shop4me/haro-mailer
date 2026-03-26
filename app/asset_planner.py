"""Asset planning: decide real-only vs concept vs text-only from journalist request text."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from app.asset_types import AssetMode, AssetPlan
from app.config import settings

if TYPE_CHECKING:
    from app.classifier import MatchResult
    from app.models import Business, HaroRequest

LOGGER = logging.getLogger(__name__)

# Hard signals: factual authenticity required — never auto-send AI concepts as real.
_REAL_PROJECT_SIGNALS: tuple[str, ...] = (
    "real projects",
    "client projects",
    "completed homes",
    "portfolio",
    "photographer credit",
    "original photography",
    "location proof",
    "us projects only",
    "u.s. projects",
    "no ai",
    "no a.i.",
    "no artificial intelligence",
    "send project images",
    "interior projects featuring",
    "designer portfolio",
    "actual installs",
    "real homes",
    "real client",
    "verified",
    "before and after",
    "on-site",
    "on site",
)

_IMAGE_ASK_SIGNALS: tuple[str, ...] = (
    "image",
    "images",
    "photo",
    "photos",
    "jpeg",
    "jpg",
    "png",
    "hi-res",
    "high res",
    "high-res",
    "visual",
    "pictures",
    "gallery",
    "screenshot",
)

_STYLING_INSPIRATION_SIGNALS: tuple[str, ...] = (
    "styling",
    "inspiration",
    "mood board",
    "concept",
    "examples of",
    "similar look",
    "aesthetic",
)


def _lower(text: str) -> str:
    return (text or "").lower()


def _has_any(hay: str, needles: tuple[str, ...]) -> bool:
    h = _lower(hay)
    return any(n in h for n in needles)


def _explicitly_declines_images(text: str) -> bool:
    """True when the journalist clearly does not want images (overrides substring 'images')."""
    low = _lower(text)
    negatives = (
        "no images",
        "no image ",
        "no image.",
        "no image,",
        "no image needed",
        "images not needed",
        "image not needed",
        "no photos",
        "photos not needed",
        "no pictures",
        "no picture",
        "no photo",
        "don't need images",
        "do not need images",
        "doesn't need images",
        "without images",
        "quote only",
        "text only",
        "no visuals needed",
        "no visual needed",
    )
    return any(n in low for n in negatives)


def prepare_assets_for_request(
    request: HaroRequest,
    match_result: MatchResult,
    business: Business,
) -> AssetPlan:
    """Hybrid: deterministic rules first, optional LLM refinement when key is set."""
    text = request.request_text or ""
    low = _lower(text)

    requires_real = _has_any(text, _REAL_PROJECT_SIGNALS)
    text_asks_for_images = _has_any(text, _IMAGE_ASK_SIGNALS) and not _explicitly_declines_images(
        text
    )
    wants_images = bool(match_result.requires_visuals) or text_asks_for_images
    styling_ok = _has_any(text, _STYLING_INSPIRATION_SIGNALS) and not requires_real

    manual_reason = ""
    ai_risk = "low"
    asset_mode = AssetMode.no_visuals
    needs_images = False
    num_target = 0

    if not wants_images and not match_result.requires_visuals:
        plan = AssetPlan(
            needs_images=False,
            images_required_in_first_reply=False,
            wants_comments="comment" in low or "quote" in low,
            requires_real_projects=False,
            requires_real_client_work=False,
            requires_geographic_verification="us only" in low or "u.s." in low,
            allowed_geography="",
            requests_original_photography=False,
            ai_risk_level="low",
            asset_mode=AssetMode.no_visuals,
            num_images_target=0,
            manual_review_required=False,
            manual_review_reason="",
            visual_brief={},
        )
        LOGGER.info(
            "asset_plan request_id=%s mode=%s needs_images=False",
            getattr(request, "id", None),
            plan.asset_mode.value,
        )
        return plan

    needs_images = True
    num_target = min(3, max(1, settings.max_inline_preview_images or 2))

    if requires_real or match_result.visual_request_confidence >= 0.7 and "portfolio" in low:
        asset_mode = AssetMode.real_only
        ai_risk = "high" if requires_real else "medium"
        if _has_any(text, ("us only", "u.s. only", "united states")):
            manual_reason = "Geographic verification may be required for assets."
    elif styling_ok or (wants_images and not requires_real):
        asset_mode = AssetMode.concept_allowed
        ai_risk = "medium"
    else:
        # Images asked but ambiguous: prefer real-first, allow concept only if policy enables
        asset_mode = AssetMode.real_only if requires_real else AssetMode.concept_allowed
        ai_risk = "medium"

    if not settings.enable_ai_concept_visuals and asset_mode == AssetMode.concept_allowed:
        asset_mode = AssetMode.real_only
        manual_reason = "AI concept visuals disabled by config; real assets only."
        ai_risk = "low"

    brief = _visual_brief_from_text(text)
    if settings.openai_api_key and settings.asset_planner_use_llm:
        brief = _refine_brief_with_llm(text, brief)

    images_first = any(
        x in low
        for x in (
            "include images in your pitch",
            "images in first",
            "photo with",
            "send photos",
        )
    )

    manual_review = False
    if asset_mode == AssetMode.real_only and requires_real:
        manual_review = False
    if requires_real and "no ai" in low:
        manual_review = manual_review or False

    plan = AssetPlan(
        needs_images=needs_images,
        images_required_in_first_reply=images_first,
        wants_comments="comment" in low or "quote" in low,
        requires_real_projects=requires_real,
        requires_real_client_work="client" in low and "project" in low,
        requires_geographic_verification="us only" in low or "u.s." in low,
        allowed_geography=_extract_geo_hint(low),
        requests_original_photography="original" in low and "photo" in low,
        ai_risk_level=ai_risk,
        asset_mode=asset_mode,
        num_images_target=num_target,
        manual_review_required=manual_review,
        manual_review_reason=manual_reason,
        visual_brief=brief,
    )
    LOGGER.info(
        "asset_plan request_id=%s mode=%s real_projects=%s manual_review=%s",
        getattr(request, "id", None),
        plan.asset_mode.value,
        plan.requires_real_projects,
        plan.manual_review_required,
    )
    return plan


def _extract_geo_hint(low: str) -> str:
    if "us only" in low or "u.s." in low:
        return "US"
    return ""


def _visual_brief_from_text(text: str) -> dict:
    """Structured fields for prompts (deterministic extraction)."""
    low = _lower(text)
    room = "living space"
    for r in ("kitchen", "bathroom", "bedroom", "patio", "outdoor", "dining"):
        if r in low:
            room = r
            break
    style = "transitional"
    for s in ("modern", "traditional", "minimal", "coastal", "farmhouse"):
        if s in low:
            style = s
            break
    return {
        "room_type": room,
        "style": style,
        "color_palette": "",
        "materials": "",
        "base_scene": "editorial interior",
        "product_cues": "",
        "camera_style": "natural daylight, 35mm editorial",
        "mood": "warm, believable",
        "avoid_terms": "cgi, surreal, warped, glossy fake render",
    }


def _refine_brief_with_llm(text: str, brief: dict) -> dict:
    """Optional LLM pass — off by default."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "Return only valid JSON with keys room_type, style, color_palette, materials, base_scene, mood, avoid_terms.",
                },
                {"role": "user", "content": "Extract visual brief from:\n" + text[:4000]},
            ],
        )
        raw = r.choices[0].message.content or "{}"
        data = json.loads(raw)
        if isinstance(data, dict):
            out = dict(brief)
            for k in brief:
                if k in data and data[k]:
                    out[k] = str(data[k])[:500]
            return out
    except Exception as exc:
        LOGGER.debug("asset_planner LLM brief skipped: %s", exc)
    return brief
