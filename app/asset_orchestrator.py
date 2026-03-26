"""Wire asset planning, discovery, generation, ranking, drafting, and metadata."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.asset_finder import find_candidate_assets
from app.asset_planner import prepare_assets_for_request
from app.asset_ranker import rank_and_select_assets
from app.asset_send_guard import should_auto_send_with_assets
from app.asset_types import AssetCandidate, AssetContext, AssetMode, AssetPlan
from app.classifier import MatchResult
from app.config import settings
from app.drafter import draft_reply
from app.image_generator import generate_candidate_images
from app.models import Business, HaroRequest

LOGGER = logging.getLogger(__name__)


def _plan_to_dict(plan: AssetPlan) -> dict[str, Any]:
    """JSON-serializable dict (no Enum objects)."""
    return {
        "needs_images": plan.needs_images,
        "images_required_in_first_reply": plan.images_required_in_first_reply,
        "wants_comments": plan.wants_comments,
        "requires_real_projects": plan.requires_real_projects,
        "requires_real_client_work": plan.requires_real_client_work,
        "requires_geographic_verification": plan.requires_geographic_verification,
        "allowed_geography": plan.allowed_geography,
        "requests_original_photography": plan.requests_original_photography,
        "ai_risk_level": plan.ai_risk_level,
        "asset_mode": plan.asset_mode.value,
        "num_images_target": plan.num_images_target,
        "manual_review_required": plan.manual_review_required,
        "manual_review_reason": plan.manual_review_reason,
        "visual_brief": plan.visual_brief,
    }


def _build_asset_context(
    plan: AssetPlan,
    selected: list[AssetCandidate],
) -> AssetContext:
    previews = selected[: settings.max_inline_preview_images]
    must_disclose = any(not a.is_real for a in selected) and plan.asset_mode == AssetMode.concept_allowed
    note = ""
    if must_disclose:
        note = "Include a brief line that attached visuals are styling concepts, not photographs of completed client projects."
    return AssetContext(
        asset_mode=plan.asset_mode,
        selected_assets=selected,
        inline_preview_assets=previews,
        full_res_link="",
        must_disclose_ai=must_disclose,
        usage_note=note,
        wants_comments=plan.wants_comments,
        images_required_in_first_reply=plan.images_required_in_first_reply,
    )


def run_asset_reply_pipeline(
    req: HaroRequest,
    business: Business,
    match: MatchResult,
) -> tuple[tuple[str, str] | None, AssetContext | None, dict[str, Any]]:
    """
    Returns (draft_pair or None, asset_context or None, reply_extras dict).
    reply_extras includes asset_plan_json, selected_asset_metadata_json, flags for Reply row.
    """
    extras: dict[str, Any] = {
        "asset_plan_json": "{}",
        "selected_asset_metadata_json": "[]",
        "attachment_paths_json": "[]",
        "inline_preview_paths_json": "[]",
        "full_res_link": "",
        "must_disclose_ai": False,
        "manual_review_required": False,
        "manual_review_reason": "",
        "asset_send_status": "TEXT_ONLY",
    }

    if not settings.enable_asset_automation:
        dp = draft_reply(req, business, asset_context=None)
        extras["asset_mode"] = None
        return dp, None, extras

    plan = prepare_assets_for_request(req, match, business)
    extras["asset_plan_json"] = json.dumps(_plan_to_dict(plan))
    extras["asset_mode"] = plan.asset_mode.value

    if plan.asset_mode == AssetMode.no_visuals or not plan.needs_images:
        ctx = None
        dp = draft_reply(req, business, asset_context=ctx)
        extras["asset_send_status"] = "TEXT_ONLY"
        return dp, ctx, extras

    candidates = find_candidate_assets(req, business, plan)
    LOGGER.info(
        "asset_orchestrator candidates_real=%s mode=%s",
        len(candidates),
        plan.asset_mode.value,
    )

    selected: list[AssetCandidate] = []
    if plan.asset_mode == AssetMode.real_only:
        selected = rank_and_select_assets(candidates, plan, max_send=settings.max_inline_preview_images)
        if not selected:
            extras["manual_review_required"] = True
            extras["manual_review_reason"] = "real_assets_required_none_found"
            extras["asset_send_status"] = "NEEDS_REAL_ASSETS"
            ctx = _build_asset_context(plan, [])
            dp = draft_reply(req, business, asset_context=ctx)
            return dp, ctx, extras
    elif plan.asset_mode == AssetMode.concept_allowed:
        selected = rank_and_select_assets(candidates, plan, max_send=settings.max_inline_preview_images)
        if not selected and settings.enable_ai_concept_visuals:
            gen = generate_candidate_images(plan, business, count=settings.max_generated_candidates)
            merged = candidates + gen
            selected = rank_and_select_assets(merged, plan, max_send=settings.max_inline_preview_images)
        if not selected:
            extras["manual_review_required"] = True
            extras["manual_review_reason"] = "no_assets_after_search_and_generation"
            extras["asset_send_status"] = "NEEDS_ASSETS"
            ctx = _build_asset_context(plan, [])
            dp = draft_reply(req, business, asset_context=ctx)
            return dp, ctx, extras

    ctx = _build_asset_context(plan, selected)
    extras["selected_asset_metadata_json"] = json.dumps(
        [
            {
                "source_type": a.source_type,
                "is_real": a.is_real,
                "path_or_url": a.path_or_url,
                "caption": a.caption,
                "total_score": a.total_score,
                "score_components": a.score_components,
            }
            for a in selected
        ]
    )
    paths = [a.path_or_url for a in ctx.inline_preview_assets if a.path_or_url and not a.path_or_url.startswith("inline_bytes:")]
    extras["inline_preview_paths_json"] = json.dumps(paths[: settings.max_inline_preview_images])
    extras["attachment_paths_json"] = json.dumps(paths)
    extras["must_disclose_ai"] = ctx.must_disclose_ai
    extras["asset_send_status"] = "READY"
    if ctx.must_disclose_ai:
        extras["full_res_link"] = ""

    dp = draft_reply(req, business, asset_context=ctx)
    ok, reason = should_auto_send_with_assets(match, plan, selected, dp[1] if dp else "")
    extras["auto_send_guard_ok"] = ok
    extras["auto_send_guard_reason"] = reason
    if not ok:
        extras["manual_review_required"] = True
        extras["manual_review_reason"] = reason
        LOGGER.warning(
            "asset_orchestrator auto_send_guard block request_id=%s reason=%s",
            req.id,
            reason,
        )

    return dp, ctx, extras


def should_force_manual_send_block(extras: dict[str, Any]) -> bool:
    return bool(extras.get("manual_review_required"))
