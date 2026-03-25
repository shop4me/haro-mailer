"""Regency AI relevance gate: mocked classifier + threshold behavior."""

from types import SimpleNamespace

from app.regency_ai_relevance import (
    REGENCY_RELEVANCE_MIN_CONFIDENCE,
    RegencyAiRelevanceResult,
    allows_regency_drafting,
    apply_regency_relevance_gate,
    classify_regency_relevance,
    is_regency_business,
    request_summary,
)


def _req(text: str, category: str = "", outlet: str = "", rid: int = 42):
    return SimpleNamespace(id=rid, request_text=text, category=category, outlet=outlet)


def _regency():
    return SimpleNamespace(
        id=7,
        name="Regency Shop",
        enabled=True,
        strict_ai_relevance_enabled=True,
        strict_ai_relevance_system_prompt="",
        strict_ai_relevance_min_confidence=0.82,
    )


def _other_biz():
    return SimpleNamespace(id=99, name="Other LLC", enabled=True)


class TestAllowsDrafting:
    def test_relevant_high_confidence(self):
        r = RegencyAiRelevanceResult("RELEVANT", REGENCY_RELEVANCE_MIN_CONFIDENCE, "ok", "furniture")
        assert allows_regency_drafting(r)

    def test_relevant_low_confidence_blocked(self):
        r = RegencyAiRelevanceResult("RELEVANT", REGENCY_RELEVANCE_MIN_CONFIDENCE - 0.01, "weak", "none")
        assert not allows_regency_drafting(r)

    def test_not_relevant_blocked(self):
        r = RegencyAiRelevanceResult("NOT_RELEVANT", 0.99, "off topic", "none")
        assert not allows_regency_drafting(r)

    def test_error_blocked(self):
        r = RegencyAiRelevanceResult("NOT_RELEVANT", 0.0, "x", "none", error="api")
        assert not allows_regency_drafting(r)


class TestApplyGate:
    def test_non_regency_unchanged(self, monkeypatch):
        def _fail(_):
            raise AssertionError("classifier should not run for non-Regency")

        monkeypatch.setattr("app.regency_ai_relevance.classify_regency_relevance", _fail)
        m, bid, reason, tags = apply_regency_relevance_gate(
            _req("anything"),
            True,
            99,
            [_other_biz()],
            "HARO",
            "heuristic",
            [],
        )
        assert m and bid == 99 and reason == "heuristic"

    def test_regency_not_relevant_no_match(self, monkeypatch):
        monkeypatch.setattr(
            "app.regency_ai_relevance.classify_regency_relevance",
            lambda r, b=None: RegencyAiRelevanceResult(
                "NOT_RELEVANT",
                0.95,
                "Healthcare story, not furniture.",
                "none",
            ),
        )
        m, bid, reason, tags = apply_regency_relevance_gate(
            _req("Parkinson's physicians for awareness month"),
            True,
            7,
            [_regency()],
            "SOS",
            "prior",
            ["home_garden"],
        )
        assert not m and bid is None
        assert "NOT_RELEVANT" in reason
        assert tags == []

    def test_regency_relevant_passes(self, monkeypatch):
        monkeypatch.setattr(
            "app.regency_ai_relevance.classify_regency_relevance",
            lambda r, b=None: RegencyAiRelevanceResult(
                "RELEVANT",
                0.9,
                "Kitchen cabinetry sizing for remodels.",
                "cabinetry",
            ),
        )
        m, bid, reason, tags = apply_regency_relevance_gate(
            _req("Standard kitchen cabinet sizes?"),
            True,
            7,
            [_regency()],
            "HARO",
            "match",
            ["home_garden"],
        )
        assert m and bid == 7
        assert "RELEVANT" in reason
        assert "strict_ai_relevant" in tags
        assert "niche_cabinetry" in tags

    def test_examples_table(self, monkeypatch):
        """Documented acceptance examples: map query -> mock decision (no live API)."""
        cases = [
            (
                "Seeking physicians for Parkinson's quotes.",
                "NOT_RELEVANT",
                0.92,
                False,
            ),
            (
                "Annuity and tax planning for retirees.",
                "NOT_RELEVANT",
                0.88,
                False,
            ),
            (
                "What are standard kitchen cabinet sizes?",
                "RELEVANT",
                0.91,
                True,
            ),
            (
                "Home office essentials, desk and chair tips.",
                "RELEVANT",
                0.89,
                True,
            ),
            (
                "Experts on rocking chairs and porch seating.",
                "RELEVANT",
                0.87,
                True,
            ),
            (
                "Sectionals too large for new construction homes.",
                "RELEVANT",
                0.86,
                True,
            ),
            (
                "Digital communities reshaping workplace collaboration.",
                "NOT_RELEVANT",
                0.9,
                False,
            ),
            (
                "Veterinarians on whether dogs should eat carrots.",
                "NOT_RELEVANT",
                0.93,
                False,
            ),
        ]

        for query, decision, conf, expect_allow in cases:
            monkeypatch.setattr(
                "app.regency_ai_relevance.classify_regency_relevance",
                lambda r, b=None, d=decision, c=conf: RegencyAiRelevanceResult(
                    d, c, "mock", "furniture" if d == "RELEVANT" else "none"
                ),
            )
            m, bid, _, _ = apply_regency_relevance_gate(
                _req(query),
                True,
                7,
                [_regency()],
                "HARO",
                "x",
                [],
            )
            assert bool(m) == expect_allow, (query, decision, expect_allow)


class TestNoApiKey:
    def test_classify_fails_closed(self, monkeypatch):
        monkeypatch.setattr("app.regency_ai_relevance.settings.openai_api_key", "")
        r = classify_regency_relevance(_req("kitchen cabinets"))
        assert r.decision == "NOT_RELEVANT"
        assert r.error


class TestHelpers:
    def test_is_regency(self):
        assert is_regency_business(SimpleNamespace(name="Regency Shop"))
        assert not is_regency_business(SimpleNamespace(name="Acme"))

    def test_request_summary_truncates(self):
        long = "x" * 200
        s = request_summary(SimpleNamespace(request_text=long, category="", outlet=""))
        assert len(s) <= 121

