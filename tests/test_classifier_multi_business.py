"""Multi-business + founder/expert classification (no OpenAI)."""

from types import SimpleNamespace

import pytest

from app.classifier import (
    _apply_founder_expert_audit_patch,
    _is_founder_expert_query,
    _reconcile_ai_decision,
    _try_founder_expert_match,
    _vertical_overlap_score,
    classify_request,
)
from app.models import Business


def _biz(bid: int, name: str, nature: str, keywords: str) -> Business:
    return Business(
        id=bid,
        name=name,
        contact_name="Founder",
        nature_of_business=nature,
        keywords=keywords,
        enabled=True,
    )


REGENCY = _biz(1, "Regency Shop", "Modular sofas and home furniture.", "sofa,furniture,home decor")
PEACHY = _biz(2, "Peachy Peaks", "Swimwear for all occasions", "swimwear,bikini,beach related")


class TestFounderExpertDetection:
    def test_swimwear_brand_founder_query(self):
        q = (
            "Looking for fashion or personal stylists, swimwear designers/brand "
            "founders to share practical advice on the most flattering swimsuit styles."
        )
        assert _is_founder_expert_query(q)

    def test_furniture_only_not_founder(self):
        assert not _is_founder_expert_query("Seeking interior designers for living room layout tips.")


class TestVerticalOverlap:
    def test_peachy_swimwear_query(self):
        q = "swimwear designers and brand founders for flattering swimsuit advice"
        assert _vertical_overlap_score(PEACHY, q) >= 0.14
        assert _vertical_overlap_score(REGENCY, q) < 0.14


class TestFounderExpertFastPath:
    def test_matches_peachy_not_regency(self):
        req = SimpleNamespace(
            request_text=(
                "Looking for swimwear designers/brand founders to share advice on "
                "flattering swimsuit styles for Rank & Style."
            ),
            category="Lifestyle and Entertainment",
            outlet="Rank & Style",
        )
        result = _try_founder_expert_match(req, [REGENCY, PEACHY], False, 0.0)
        assert result is not None
        assert result.matched is True
        assert result.matched_business_id == 2
        assert "founder_expert" in result.topic_tags
        peachy_audit = next(r for r in result.per_business_audit if r["business_id"] == 2)
        regency_audit = next(r for r in result.per_business_audit if r["business_id"] == 1)
        assert peachy_audit["relevant"] is True
        assert regency_audit["relevant"] is False


class TestAuditReconcile:
    def test_overrides_wrong_global_no_match(self):
        ai = {
            "matched": False,
            "matched_business_id": None,
            "confidence": 0.1,
            "reasoning_short": "Unrelated to home lifestyle.",
            "topic_tags": [],
        }
        audit = [
            {
                "business_id": 1,
                "name": "Regency Shop",
                "relevant": False,
                "reason": "Not swimwear.",
                "source": "ai_classifier",
            },
            {
                "business_id": 2,
                "name": "Peachy Peaks",
                "relevant": True,
                "reason": "Swimwear founder query.",
                "source": "ai_classifier",
            },
        ]
        heur = {1: 0.0, 2: 0.36}
        matched, bid, conf, _ = _reconcile_ai_decision(ai, audit, heur, [REGENCY, PEACHY])
        assert matched is True
        assert bid == 2
        assert conf > 0.3

    def test_founder_patch_fixes_ai_audit(self):
        req = SimpleNamespace(
            request_text="Seeking swimwear brand founders for expert quotes on bikini fit.",
        )
        audit = [
            {
                "business_id": 2,
                "name": "Peachy Peaks",
                "relevant": False,
                "reason": "Wrong AI reason.",
                "source": "ai_classifier",
            },
        ]
        patched = _apply_founder_expert_audit_patch(req, audit, [PEACHY])
        assert patched[0]["relevant"] is True
        assert patched[0]["source"] == "founder_expert_patch"


class TestClassifyRequestFounderPath:
    def test_classify_swimwear_founder_without_openai(self, monkeypatch):
        monkeypatch.setattr("app.classifier.settings.openai_api_key", "")

        req = SimpleNamespace(
            request_text=(
                "Summary: swimwear designers/brand founders for flattering swimsuit styles. "
                "Rank & Style deadline tomorrow."
            ),
            category="Lifestyle and Entertainment",
            outlet="Rank & Style",
        )
        result = classify_request(req, [REGENCY, PEACHY], inbound_source=None)
        assert result.matched is True
        assert result.matched_business_id == 2
