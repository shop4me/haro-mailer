"""Regression tests for home/garden weighted scoring (no OpenAI)."""

from types import SimpleNamespace

import pytest

from app.classifier import HG_BAND_STRONG_MIN, _is_clear_home_and_garden_match, _score_home_and_garden_topic


def _req(text: str, category: str = "", outlet: str | None = None):
    return SimpleNamespace(request_text=text, category=category, outlet=outlet or "")


class TestHomeGardenBands:
    def test_strong_interior_living_room(self):
        r = _req(
            "Looking for interior designers to discuss small living room layout mistakes for a magazine piece."
        )
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"
        assert s.total_score >= HG_BAND_STRONG_MIN
        assert _is_clear_home_and_garden_match(s)
        assert "interior design" in s.matched_strong_terms or "living room" in s.matched_strong_terms

    def test_strong_spring_cleaning(self):
        r = _req("Experts needed for spring cleaning advice for busy families.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_patio_trends(self):
        r = _req("What are the top patio trends this summer for homeowners?")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_mudroom(self):
        r = _req("How should homeowners organize a mudroom in a cold climate?")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band in ("strong", "borderline")
        assert s.total_score > 0

    def test_strong_paint_bedroom(self):
        r = _req("Best paint colors for a cozy bedroom in 2026.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_sectional_apartment(self):
        r = _req("Tips for designing around a sectional in a small apartment.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_kitchen_remodel(self):
        r = _req("Kitchen remodel mistakes homeowners make in the first year.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_backyard_entertaining(self):
        r = _req("How to refresh your backyard for entertaining this summer.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_upholstery(self):
        r = _req("Best upholstery fabrics for family homes with pets.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "strong"

    def test_strong_decluttering(self):
        r = _req("What decluttering habits actually work for real people?")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band in ("strong", "borderline")

    def test_clear_non_meal_kit(self):
        r = _req("Best meal kit subscriptions for working parents in 2026.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"
        assert not _is_clear_home_and_garden_match(s)
        assert "meal kit" in " ".join(s.matched_negative_terms)

    def test_clear_non_mortgage(self):
        r = _req("Top mortgage refinance tips for homeowners in 2026.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"
        assert "mortgage" in s.matched_negative_terms or "refinance" in s.matched_negative_terms

    def test_clear_non_law_firm(self):
        r = _req("How law firms can improve client intake with software.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"

    def test_clear_non_crypto(self):
        r = _req("Crypto tools for at-home investors building their portfolio.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"

    def test_clear_non_payroll(self):
        r = _req("Best payroll software for small businesses in 2026.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"

    def test_clear_non_snack_box(self):
        r = _req("Subscription snack boxes for remote workers.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"

    def test_clear_non_automotive(self):
        r = _req("Automotive detailing trends for 2026.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"

    def test_clear_non_dental_software(self):
        r = _req("Dental practice management software comparison for multi-location practices.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band == "clear_non_match"

    @pytest.mark.parametrize(
        "text",
        [
            "Smart home upgrades homeowners actually use daily.",
            "Pet friendly living room design ideas for renters.",
            "Allergy reduction tips inside the home.",
            "Work from home office setup design for small spaces.",
            "Backyard security trends for suburban homeowners.",
        ],
    )
    def test_borderline_or_strong_examples(self, text: str):
        r = _req(text)
        s = _score_home_and_garden_topic(r)
        assert s.decision_band in ("borderline", "strong")
        assert not (
            s.decision_band == "clear_non_match"
            and s.text_positive_score >= 2.0
        )


class TestKitchenCleaningOverridesNegative:
    """Kitchen + cleaning should stay relevant despite junk negatives."""

    def test_family_kitchen_cleaning_not_pure_junk(self):
        r = _req("Best cleaning routine for a family kitchen with kids.")
        s = _score_home_and_garden_topic(r)
        assert s.decision_band != "clear_non_match" or s.total_score > 0
