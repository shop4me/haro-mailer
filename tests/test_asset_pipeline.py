"""Asset planning, guardrails, and drafter asset context."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.asset_planner import prepare_assets_for_request
from app.config import settings
from app.asset_send_guard import should_auto_send_with_assets
from app.asset_types import AssetMode
from app.classifier import MatchResult
from app.drafter import draft_reply
from app.models import HaroRequest


def _biz():
    return SimpleNamespace(
        id=1,
        name="Test Co",
        nature_of_business="furniture",
        keywords="sofa",
        brand_voice="warm",
        website_url="https://floatfire.com",
        contact_name="A",
        signature="Sig",
    )


def test_no_image_query_no_visuals():
    req = MagicMock(spec=HaroRequest)
    req.request_text = "Looking for a short quote on spring trends. No images needed."
    req.id = 1
    m = MatchResult(True, 1, 0.9, "ok", [], False, 0.0)
    plan = prepare_assets_for_request(req, m, _biz())
    assert plan.asset_mode == AssetMode.no_visuals


def test_real_portfolio_signals_real_only():
    req = MagicMock(spec=HaroRequest)
    req.request_text = "Send photos of real client projects and completed homes in the US only."
    req.id = 2
    m = MatchResult(True, 1, 0.9, "ok", [], True, 0.8)
    plan = prepare_assets_for_request(req, m, _biz())
    assert plan.asset_mode == AssetMode.real_only
    assert plan.requires_real_projects is True


def test_styling_inspiration_concept_allowed():
    req = MagicMock(spec=HaroRequest)
    req.request_text = "Seeking styling inspiration and mood-board style visuals for a coastal living room."
    req.id = 3
    m = MatchResult(True, 1, 0.9, "ok", [], True, 0.6)
    # Planner maps concept_allowed to real_only when AI concepts are disabled globally.
    with patch.object(settings, "enable_ai_concept_visuals", True):
        plan = prepare_assets_for_request(req, m, _biz())
    assert plan.asset_mode == AssetMode.concept_allowed


def test_guard_blocks_ai_when_real_required():
    from app.asset_types import AssetCandidate, AssetPlan

    plan = AssetPlan(
        needs_images=True,
        images_required_in_first_reply=False,
        wants_comments=False,
        requires_real_projects=True,
        requires_real_client_work=False,
        requires_geographic_verification=False,
        allowed_geography="",
        requests_original_photography=False,
        ai_risk_level="high",
        asset_mode=AssetMode.real_only,
        num_images_target=2,
        manual_review_required=False,
        manual_review_reason="",
        visual_brief={},
    )
    ai_only = [
        AssetCandidate(
            source_type="ai_generated",
            is_real=False,
            is_verified=False,
            path_or_url="",
            caption="",
            alt_description="",
            geography="",
            project_type="",
            resolution="",
        )
    ]
    mr = MatchResult(True, 1, 0.9, "ok", [])
    ok, reason = should_auto_send_with_assets(mr, plan, ai_only, "We can help with images.")
    assert ok is False
    assert "real" in reason.lower() or "ai" in reason.lower()


def test_drafter_no_visuals_no_asset_block():
    req = MagicMock(spec=HaroRequest)
    req.request_text = "Quote only on trends."
    req.journalist_name = "Jane"
    req.outlet = "Mag"
    req.category = "Home"
    req.id = 99
    pair = draft_reply(req, _biz(), asset_context=None)
    assert pair
    subj, body = pair
    assert subj
    assert "styling concept" not in (body or "").lower()


def test_matchresult_defaults_backward_compat():
    m = MatchResult(False, None, 0.0, "x", [])
    assert m.requires_visuals is False
    assert m.visual_request_confidence == 0.0
