"""Auto-send guardrails when assets are involved."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.asset_types import AssetCandidate, AssetContext, AssetMode, AssetPlan
from app.config import settings

if TYPE_CHECKING:
    from app.classifier import MatchResult

LOGGER = logging.getLogger(__name__)


def draft_contradicts_asset_reality(body: str, selected: list[AssetCandidate]) -> bool:
    """True if body claims real installs while only AI concepts exist."""
    low = (body or "").lower()
    if not selected:
        return False
    all_ai = all(not a.is_real for a in selected)
    if not all_ai:
        return False
    bad = (
        "our recent install",
        "our client's home",
        "we completed",
        "actual project",
        "real home we",
    )
    return any(b in low for b in bad)


def should_auto_send_with_assets(
    match_result: MatchResult,
    asset_plan: AssetPlan,
    selected_assets: list[AssetCandidate],
    draft_body: str,
) -> tuple[bool, str]:
    """
    Returns (allowed, reason). When allowed is False, caller should keep DRAFT / manual review.
    """
    if asset_plan.manual_review_required:
        return False, "manual_review_required: %s" % (asset_plan.manual_review_reason or "policy")

    # Real projects required but only AI available
    if asset_plan.requires_real_projects or asset_plan.asset_mode == AssetMode.real_only:
        real = [a for a in selected_assets if a.is_real]
        ai_only = [a for a in selected_assets if not a.is_real]
        if not real and ai_only:
            return False, "real_projects_required_only_ai_available"

    if asset_plan.requires_geographic_verification:
        verified = [a for a in selected_assets if a.is_verified]
        if selected_assets and not verified:
            return False, "geography_unverified_assets"

    low = (match_result.reasoning_short or "").lower() + " " + (draft_body or "").lower()
    if re.search(r"\bno\s+ai\b", low) or "no artificial intelligence" in low:
        if any(not a.is_real for a in selected_assets):
            return False, "query_says_no_ai_but_ai_assets_selected"

    if asset_plan.images_required_in_first_reply and len(selected_assets) < 1:
        return False, "images_required_but_too_few_selected"

    if draft_contradicts_asset_reality(draft_body, selected_assets):
        return False, "draft_contradicts_asset_reality"

    if any(not a.is_real for a in selected_assets) and not settings.auto_send_concept_visuals:
        return False, "auto_send_concept_visuals_disabled"

    if (
        selected_assets
        and all(a.is_real for a in selected_assets)
        and not settings.auto_send_real_assets
        and asset_plan.asset_mode == AssetMode.real_only
    ):
        return False, "auto_send_real_assets_disabled"

    return True, "ok"
