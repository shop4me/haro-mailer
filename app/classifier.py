import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from openai import OpenAI

from app.config import settings
from app.models import Business, HaroRequest
from app.regency_niche_gate import apply_regency_niche_gate_to_match

LOGGER = logging.getLogger(__name__)

# --- Home & garden scoring (tunable) -----------------------------------------

# Field weights: request text matters most; outlet is light (often noisy).
HG_TEXT_WEIGHT = 1.0
HG_CATEGORY_WEIGHT = 0.55
HG_OUTLET_WEIGHT = 0.28

HG_STRONG_POINTS = 3.0
HG_MEDIUM_POINTS = 1.4
HG_WEAK_POINTS = 0.35

# Negative phrases reduce the score; magnitude is per hit (greedy non-overlap).
HG_NEGATIVE_STRONG = -4.5
HG_NEGATIVE_MEDIUM = -2.2

# After positives from the request TEXT only, cap how much negatives can pull down (recall-first).
HG_NEG_CAP_POS_TEXT_GE_9 = 4.0
HG_NEG_CAP_POS_TEXT_GE_6 = 6.5
HG_NEG_CAP_POS_TEXT_GE_4 = 8.5

# Band thresholds on NET score (after negative cap).
# One clear strong phrase (3.0 pts in request text) should qualify — recall-first policy match.
HG_BAND_STRONG_MIN = 3.0
HG_BAND_BORDERLINE_MIN = 0.85
HG_BAND_REJECT_MAX = 0.35  # at or below this net AND weak text → clear non-match

# If request text alone has enough signal, prefer borderline (route to AI) over hard reject.
HG_TEXT_MIN_FOR_BORDERLINE = 2.0
HG_TEXT_CLEAR_REJECT_MAX = 1.4  # below this text score, allow clear non-match when net is bad


def _sort_phrases_longest_first(phrases: tuple[str, ...]) -> list[str]:
    return sorted(phrases, key=lambda p: (-len(p), p))


def _greedy_phrase_hits(text_lower: str, phrases: list[str], weight: float) -> tuple[float, list[str]]:
    """Sum weights for non-overlapping substring hits; phrases should be sorted longest-first."""
    if not text_lower or not weight:
        return 0.0, []
    n = len(text_lower)
    covered = [False] * n
    hits: list[str] = []
    total = 0.0
    for phrase in phrases:
        if not phrase:
            continue
        plen = len(phrase)
        start = 0
        while True:
            i = text_lower.find(phrase, start)
            if i < 0:
                break
            if any(covered[i : i + plen]):
                start = i + 1
                continue
            for j in range(i, i + plen):
                covered[j] = True
            total += weight
            hits.append(phrase)
            start = i + plen
    return total, hits


def _greedy_on_uncovered(
    text_lower: str, covered: list[bool], phrases: list[str], weight: float
) -> tuple[float, list[str]]:
    if not text_lower or not weight:
        return 0.0, []
    hits: list[str] = []
    total = 0.0
    for phrase in phrases:
        if not phrase:
            continue
        plen = len(phrase)
        start = 0
        while True:
            i = text_lower.find(phrase, start)
            if i < 0:
                break
            if any(covered[i : i + plen]):
                start = i + 1
                continue
            for j in range(i, i + plen):
                covered[j] = True
            total += weight
            hits.append(phrase)
            start = i + plen
    return total, hits


# Strong: clear home / garden / interior / remodel / lifestyle-at-home expertise.
_HG_STRONG: tuple[str, ...] = (
    "interior design",
    "interior designer",
    "interior decorating",
    "interior styling",
    "home decor",
    "home décor",
    "homedecor",
    "furniture trends",
    "furniture design",
    "patio furniture",
    "outdoor furniture",
    "outdoor living",
    "outdoor entertaining",
    "modular seating",
    "sectional sofa",
    "sectional couch",
    "sectional",
    "patio trends",
    "living room",
    "living space",
    "bedroom design",
    "bedroom refresh",
    "dining room",
    "kitchen remodel",
    "kitchen refresh",
    "kitchen design",
    "bathroom remodel",
    "bathroom refresh",
    "home staging",
    "home trends",
    "home refresh",
    "small space design",
    "small apartment",
    "paint colors",
    "wall color",
    "flooring",
    "hardwood floor",
    "laminate flooring",
    "upholstery",
    "upholstery fabric",
    "spring cleaning",
    "deep cleaning",
    "decluttering",
    "declutter",
    "mudroom",
    "pantry organization",
    "closet organization",
    "storage ideas",
    "home organization",
    "home improvement",
    "remodeling",
    "renovation",
    "renovate",
    "home renovation",
    "lawn care",
    "lawn and garden",
    "vegetable garden",
    "herb garden",
    "flower bed",
    "gardening",
    "gardener",
    "landscaping",
    "landscaper",
    "irrigation",
    "mulch",
    "pergola",
    "deck",
    "patio",
    "backyard",
    "yard",
    "curb appeal",
    "home maintenance",
    "seasonal decor",
    "seasonal home",
    "holiday decor",
    "window treatments",
    "window treatment",
    "area rug",
    "area rugs",
    "lighting design",
    "accent chair",
    "chesterfield",
    "loveseat",
    "coffee table",
    "dining table",
    "countertops",
    "backsplash",
    "open floor plan",
    "open-concept",
    "open concept",
    "smart home upgrades",
    "pet friendly living",
    "allergy reduction",
    "work from home office",
    "home office design",
    "home office setup",
)

# Medium: broader but still on-theme.
_HG_MEDIUM: tuple[str, ...] = (
    "furniture",
    "furnishings",
    "furnishing",
    "decor",
    "décor",
    "design trends",
    "room layout",
    "room design",
    "apartment styling",
    "apartment design",
    "cleaning tips",
    "cleaning routine",
    "household",
    "housekeeping",
    "organizing",
    "organization",
    "storage",
    "shelving",
    "shelving ideas",
    "entertaining at home",
    "hosting at home",
    "backyard entertaining",
    "patio ideas",
    "garden",
    "gardener",
    "lawn",
    "yard care",
    "outdoor space",
    "home lifestyle",
    "homeowner",
    "home owners",
    "family kitchen",
    "kitchen",
    "bathroom",
    "bedroom",
    "remodel",
    "refresh",
    "renovate",
    "staging",
    "countertop",
    "cabinets",
    "closet",
    "pantry",
    "laundry room",
    "mud room",
    "loft",
    "condo design",
    "townhome",
    "suburban home",
    "suburban",
    "suburbs",
    "decorating",
    "style a",
    "styling a",
    "design mistakes",
    "cozy bedroom",
    "cozy living",
)

# Weak: tiny nudge only (recall); applied after strong/medium via separate coverage.
_WEAK_WORDS: tuple[str, ...] = (
    "room",
    "house",
    "space",
    "outdoor",
    "home",
    "yard",
    "patio",
)

# Negatives: junk sectors (substring match, longest-first within tier).
_HG_NEG_STRONG: tuple[str, ...] = (
    "meal kit",
    "meal kits",
    "subscription box",
    "subscription snack",
    "snack box",
    "crypto",
    "cryptocurrency",
    "blockchain",
    "defi",
    "forex",
    "sportsbook",
    "gambling",
    "casino",
    "mortgage",
    "refinance",
    "refinancing",
    "home loan",
    "home equity line",
    "heloc",
    "payroll software",
    "payroll system",
    "crm software",
    "marketing automation",
    "recruiting software",
    "applicant tracking",
    "saas",
    "b2b software",
    "accounting software",
    "dental practice management",
    "medical device",
    "dental billing",
    "automotive",
    "trucking",
    "fleet management",
    "industrial equipment",
    "e-commerce platform",
    "ecommerce platform",
    "law firm",
    "legal services",
    "litigation",
    "class action",
    "supplement",
    "supplements",
    "nutraceutical",
    "skincare brand",
    "crypto tools",
    "payroll",
)

_HG_NEG_MEDIUM: tuple[str, ...] = (
    "insurance",
    "home warranty",
    "warranty plan",
    "cybersecurity",
    "malware",
    "ransomware",
    "software development",
    "app development",
    "mobile app",
    "b2b marketing",
    "demand gen",
    "investing",
    "investment tips",
    "stock tips",
    "mutual fund",
    "recruiting",
    "hiring software",
    "remote work software",
    "collaboration software",
    "vpn",
    "saas tool",
    "ai app",
    "generative ai",
    "machine learning platform",
)

_HG_STRONG_SORTED = _sort_phrases_longest_first(_HG_STRONG)
_HG_MEDIUM_SORTED = _sort_phrases_longest_first(_HG_MEDIUM)
_HG_NEG_STRONG_SORTED = _sort_phrases_longest_first(_HG_NEG_STRONG)
_HG_NEG_MEDIUM_SORTED = _sort_phrases_longest_first(_HG_NEG_MEDIUM)


def _score_positive_field(text_lower: str) -> tuple[float, list[str], list[str], list[str]]:
    """Returns weighted raw score and hit lists for strong/medium/weak tiers (non-overlapping)."""
    if not text_lower:
        return 0.0, [], [], []
    covered = [False] * len(text_lower)
    strong_s, strong_h = _greedy_on_uncovered(text_lower, covered, _HG_STRONG_SORTED, HG_STRONG_POINTS)
    medium_s, medium_h = _greedy_on_uncovered(text_lower, covered, _HG_MEDIUM_SORTED, HG_MEDIUM_POINTS)
    weak_hits: list[str] = []
    weak_total = 0.0
    for token in _WEAK_WORDS:
        for m in re.finditer(rf"\b{re.escape(token)}\b", text_lower):
            a, b = m.span()
            if any(covered[a:b]):
                continue
            for k in range(a, b):
                covered[k] = True
            weak_total += HG_WEAK_POINTS
            weak_hits.append(token)
    total = strong_s + medium_s + weak_total
    return total, strong_h, medium_h, weak_hits


def _score_negative_field(text_lower: str) -> tuple[float, list[str]]:
    s1, h1 = _greedy_phrase_hits(text_lower, _HG_NEG_STRONG_SORTED, -HG_NEGATIVE_STRONG)
    s2, h2 = _greedy_phrase_hits(text_lower, _HG_NEG_MEDIUM_SORTED, -HG_NEGATIVE_MEDIUM)
    # s1,s2 are negative numbers; magnitude for cap: use abs
    return s1 + s2, h1 + h2


@dataclass
class HomeGardenScoreResult:
    """Explainable home/garden heuristic result (before AI / policy routing)."""

    total_score: float  # alias in logs: home_garden_score
    raw_positive_score: float
    raw_negative_magnitude: float
    matched_strong_terms: list[str] = field(default_factory=list)
    matched_medium_terms: list[str] = field(default_factory=list)
    matched_weak_terms: list[str] = field(default_factory=list)
    matched_negative_terms: list[str] = field(default_factory=list)
    text_positive_score: float = 0.0
    category_positive_score: float = 0.0
    outlet_positive_score: float = 0.0
    text_negative_score: float = 0.0
    category_negative_score: float = 0.0
    outlet_negative_score: float = 0.0
    decision_band: Literal["strong", "borderline", "clear_non_match"] = "clear_non_match"
    notes: str = ""

    @property
    def home_garden_score(self) -> float:
        return self.total_score

    @property
    def final_decision_band(self) -> Literal["strong", "borderline", "clear_non_match"]:
        return self.decision_band

    def log_summary(self) -> str:
        return (
            f"band={self.decision_band} net={self.total_score:.2f} "
            f"(+{self.raw_positive_score:.2f} / -{self.raw_negative_magnitude:.2f}) "
            f"text_pos={self.text_positive_score:.2f} "
            f"strong={self.matched_strong_terms[:8]}{'…' if len(self.matched_strong_terms) > 8 else ''} "
            f"neg={self.matched_negative_terms[:8]}{'…' if len(self.matched_negative_terms) > 8 else ''}"
        )


def _score_home_and_garden_topic(request: HaroRequest) -> HomeGardenScoreResult:
    text = (request.request_text or "").lower()
    cat = (request.category or "").lower()
    out = (request.outlet or "").lower()

    ts, t_strong, t_medium, t_weak = _score_positive_field(text)
    cs, c_strong, c_medium, c_weak = _score_positive_field(cat)
    os, o_strong, o_medium, o_weak = _score_positive_field(out)

    tn, t_neg_hits = _score_negative_field(text)
    cn, c_neg_hits = _score_negative_field(cat)
    on, o_neg_hits = _score_negative_field(out)

    pos_raw = (
        ts * HG_TEXT_WEIGHT
        + cs * HG_CATEGORY_WEIGHT
        + os * HG_OUTLET_WEIGHT
    )
    neg_raw_mag = abs(
        tn * HG_TEXT_WEIGHT
        + cn * HG_CATEGORY_WEIGHT
        + on * HG_OUTLET_WEIGHT
    )

    # Recall-first: cap how much negatives hurt when request text has real home signals.
    pos_text_only = ts
    if pos_text_only >= 9.0:
        neg_eff = min(neg_raw_mag, HG_NEG_CAP_POS_TEXT_GE_9)
    elif pos_text_only >= 6.0:
        neg_eff = min(neg_raw_mag, HG_NEG_CAP_POS_TEXT_GE_6)
    elif pos_text_only >= 4.0:
        neg_eff = min(neg_raw_mag, HG_NEG_CAP_POS_TEXT_GE_4)
    else:
        neg_eff = neg_raw_mag

    net = pos_raw - neg_eff

    strong_all = t_strong + c_strong + o_strong
    medium_all = t_medium + c_medium + o_medium
    weak_all = t_weak + c_weak + o_weak
    neg_all = t_neg_hits + c_neg_hits + o_neg_hits

    notes = ""
    if neg_raw_mag > neg_eff + 0.01:
        notes = f"negative_penalty_capped raw_neg={neg_raw_mag:.2f} eff_neg={neg_eff:.2f}"

    band: Literal["strong", "borderline", "clear_non_match"]
    if net >= HG_BAND_STRONG_MIN:
        band = "strong"
    elif net <= HG_BAND_REJECT_MAX and pos_text_only < HG_TEXT_CLEAR_REJECT_MAX:
        band = "clear_non_match"
    elif net >= HG_BAND_BORDERLINE_MIN or pos_text_only >= HG_TEXT_MIN_FOR_BORDERLINE:
        band = "borderline"
    else:
        band = "clear_non_match"

    return HomeGardenScoreResult(
        total_score=net,
        raw_positive_score=pos_raw,
        raw_negative_magnitude=neg_raw_mag,
        matched_strong_terms=sorted(set(strong_all)),
        matched_medium_terms=sorted(set(medium_all)),
        matched_weak_terms=sorted(set(weak_all)),
        matched_negative_terms=sorted(set(neg_all)),
        text_positive_score=ts,
        category_positive_score=cs,
        outlet_positive_score=os,
        text_negative_score=tn,
        category_negative_score=cn,
        outlet_negative_score=on,
        decision_band=band,
        notes=notes,
    )


def _is_clear_home_and_garden_match(score_result: HomeGardenScoreResult) -> bool:
    return score_result.decision_band == "strong"


def _is_borderline_home_and_garden_match(score_result: HomeGardenScoreResult) -> bool:
    return score_result.decision_band == "borderline"


@dataclass
class MatchResult:
    matched: bool
    matched_business_id: int | None
    confidence: float
    reasoning_short: str
    topic_tags: list[str]
    # Lightweight hints for asset_planner (business relevance stays in classify_request)
    requires_visuals: bool = False
    visual_request_confidence: float = 0.0
    # One row per enabled business: relevance + reason (AI, heuristic, policy, or strict_ai_relevance)
    per_business_audit: list[dict] = field(default_factory=list)


def _visual_request_hints_from_text(text: str) -> tuple[bool, float]:
    """Heuristic only; asset_planner does full decisioning."""
    t = (text or "").lower()
    score = 0.0
    for k in (
        "image",
        "images",
        "photo",
        "photos",
        "jpeg",
        "png",
        "jpg",
        "hi-res",
        "high res",
        "visual",
        "picture",
        "gallery",
        "screenshot",
        "portfolio images",
    ):
        if k in t:
            score += 0.08
    score = min(1.0, score)
    return score >= 0.35, score


def _audit_policy_all(enabled: list[Business], reason: str) -> list[dict]:
    return [
        {
            "business_id": b.id,
            "name": (b.name or "").strip() or ("Business %s" % b.id),
            "relevant": False,
            "reason": reason,
            "source": "policy",
        }
        for b in enabled
    ]


def _audit_home_garden_routing(enabled: list[Business], chosen_id: int, policy_note: str) -> list[dict]:
    out = []
    for b in enabled:
        if b.id == chosen_id:
            out.append(
                {
                    "business_id": b.id,
                    "name": (b.name or "").strip() or ("Business %s" % b.id),
                    "relevant": True,
                    "reason": policy_note,
                    "source": "home_garden_policy",
                }
            )
        else:
            out.append(
                {
                    "business_id": b.id,
                    "name": (b.name or "").strip() or ("Business %s" % b.id),
                    "relevant": False,
                    "reason": "Not selected — home/garden strong-path routes to the designated home & garden business only.",
                    "source": "home_garden_policy",
                }
            )
    return out


def _normalize_per_business_audit(raw: object, enabled: list[Business]) -> list[dict]:
    by_id = {b.id: b for b in enabled}
    out: list[dict] = []
    seen: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            bid = item.get("business_id")
            if bid is None:
                continue
            try:
                bid = int(bid)
            except (TypeError, ValueError):
                continue
            if bid not in by_id:
                continue
            seen.add(bid)
            b = by_id[bid]
            out.append(
                {
                    "business_id": bid,
                    "name": (b.name or "").strip() or ("Business %s" % bid),
                    "relevant": bool(item.get("relevant")),
                    "reason": str(item.get("reason") or "")[:500],
                    "source": str(item.get("source") or "ai_classifier")[:64],
                }
            )
    for b in enabled:
        if b.id not in seen:
            out.append(
                {
                    "business_id": b.id,
                    "name": (b.name or "").strip() or ("Business %s" % b.id),
                    "relevant": False,
                    "reason": "No per-business line in model output (filled by system).",
                    "source": "ai_fallback",
                }
            )
    return out


def _audit_from_heuristic_scores(scores: dict[int, float], enabled: list[Business]) -> list[dict]:
    best_id: int | None = None
    best_s = -1.0
    if scores:
        best_id, best_s = max(scores.items(), key=lambda kv: kv[1])
    out = []
    for b in enabled:
        s = scores.get(b.id, 0.0)
        strong = best_s >= 0.2 and best_id is not None and b.id == best_id
        rel = strong
        reason = "Keyword/heuristic score %.2f — %s" % (
            s,
            "best match among businesses" if strong else "not selected as best match",
        )
        if not scores:
            reason = "No keyword overlap with configured business terms."
            rel = False
        elif best_s < 0.2:
            reason = "No reliable keyword match (best score %.2f)." % best_s
            rel = False
        out.append(
            {
                "business_id": b.id,
                "name": (b.name or "").strip() or ("Business %s" % b.id),
                "relevant": rel,
                "reason": reason,
                "source": "keyword_heuristic",
            }
        )
    return out


def _apply_regency_niche_gate_result(
    request: HaroRequest,
    result: MatchResult,
    enabled: list[Business],
    inbound_source: str | None,
) -> MatchResult:
    m, bid, reason, tags, audit = apply_regency_niche_gate_to_match(
        request,
        result.matched,
        result.matched_business_id,
        enabled,
        inbound_source,
        result.reasoning_short,
        result.topic_tags,
        result.per_business_audit,
    )
    if not m:
        return MatchResult(
            False,
            None,
            0.0,
            reason,
            [],
            result.requires_visuals,
            result.visual_request_confidence,
            audit,
        )
    return MatchResult(
        m,
        bid,
        result.confidence,
        reason,
        tags,
        result.requires_visuals,
        result.visual_request_confidence,
        audit,
    )


def classify_request(
    request: HaroRequest,
    businesses: list[Business],
    inbound_source: str | None = None,
) -> MatchResult:
    hv, hc = _visual_request_hints_from_text(request.request_text or "")
    enabled = [b for b in businesses if b.enabled]
    if not enabled:
        return MatchResult(False, None, 0.0, "No enabled businesses configured.", [], hv, hc, [])

    # We never appear in person for anyone.
    if _requires_in_person(request.request_text):
        return MatchResult(
            False,
            None,
            0.0,
            "Query requires in-person participation; we do not appear in person.",
            [],
            hv,
            hc,
            _audit_policy_all(enabled, "Policy: we do not appear in person."),
        )
    # We don't send products/gifts except for TV stations.
    if _requires_products_or_gifts(request.request_text) and not _is_tv_station(request.outlet):
        return MatchResult(
            False,
            None,
            0.0,
            "Query requires sending/giving products or gifts; we only do this for TV stations.",
            [],
            hv,
            hc,
            _audit_policy_all(
                enabled,
                "Policy: we do not send products or gifts except for TV station outlets.",
            ),
        )

    hg = _score_home_and_garden_topic(request)
    LOGGER.info("Home/garden heuristic: %s", hg.log_summary())
    if hg.notes:
        LOGGER.info("Home/garden scorer notes: %s", hg.notes)

    # Policy: only immediate home_garden tag when heuristic is STRONG (not brittle keyword-only).
    if _is_clear_home_and_garden_match(hg):
        hg_business_id = _resolve_home_garden_business(enabled)
        if hg_business_id is not None:
            hg_audit = _audit_home_garden_routing(
                enabled,
                hg_business_id,
                "Home and garden topic — designated business for this policy path.",
            )
            return _apply_regency_niche_gate_result(
                request,
                MatchResult(
                    True,
                    hg_business_id,
                    0.95,
                    "Home and garden topic — always respond (policy, strong heuristic).",
                    ["home_garden"],
                    hv,
                    hc,
                    hg_audit,
                ),
                enabled,
                inbound_source,
            )
        LOGGER.warning(
            "Home/garden strong band but no business resolved; set HOME_GARDEN_BUSINESS_ID or tune businesses. %s",
            hg.log_summary(),
        )

    heuristic_scores = _heuristic_scores(request.request_text, enabled)
    founder_match = _try_founder_expert_match(request, enabled, hv, hc)
    ai_result = _classify_with_openai(request, enabled, hg_score_result=hg)

    if not ai_result:
        if founder_match:
            return _apply_regency_niche_gate_result(
                request, founder_match, enabled, inbound_source
            )
        return _apply_regency_niche_gate_result(
            request,
            _select_from_heuristic(heuristic_scores, enabled, hv, hc),
            enabled,
            inbound_source,
        )

    audit = _normalize_per_business_audit(ai_result.get("per_business_audit"), enabled)
    audit = _apply_founder_expert_audit_patch(request, audit, enabled)
    matched, chosen_id, blended, reasoning = _reconcile_ai_decision(
        ai_result, audit, heuristic_scores, enabled
    )
    if not matched and founder_match:
        return _apply_regency_niche_gate_result(
            request, founder_match, enabled, inbound_source
        )

    tags = ai_result.get("topic_tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags if t is not None]
    if matched and _is_founder_expert_query(request.request_text or "") and "founder_expert" not in tags:
        tags.append("founder_expert")
    return _apply_regency_niche_gate_result(
        request,
        MatchResult(
            matched,
            chosen_id,
            max(0.0, min(1.0, blended)),
            reasoning,
            tags,
            hv,
            hc,
            audit,
        ),
        enabled,
        inbound_source,
    )


def _requires_in_person(text: str) -> bool:
    """True if the request clearly requires physical / in-person participation.

    We do not block remote phone or video (those are not 'in person'). We avoid
    loose phrases like 'visit our' (often means website) or 'phone interview'
    (usually still answerable in writing).
    """
    if not (text or "").strip():
        return False
    lower = text.lower()
    return (
        "in person" in lower
        or "in-person" in lower
        or "appear in person" in lower
        or "in studio" in lower
        or "on site" in lower
        or "on-site" in lower
        or "come to our office" in lower
        or "come to our studio" in lower
        or "meet in person" in lower
        or "face to face" in lower
        or "face-to-face" in lower
    )


def _requires_products_or_gifts(text: str) -> bool:
    """True if the request clearly requires sending or giving products, samples, or gifts."""
    if not (text or "").strip():
        return False
    lower = text.lower()
    # Avoid matching unrelated words (e.g. 'gifted') via whole-word 'gift(s)'
    gift_word = bool(re.search(r"\bgifts?\b", lower))
    return (
        "send a sample" in lower
        or "send samples" in lower
        or "product sample" in lower
        or "product samples" in lower
        or "free product" in lower
        or "free products" in lower
        or "complimentary product" in lower
        or "send product" in lower
        or gift_word
        or "give away" in lower
        or "giveaway" in lower
    )


def _resolve_regency_shop_id(businesses: list[Business]) -> int | None:
    """Prefer a business whose name is Regency Shop (case insensitive) when set in the DB."""
    for b in businesses:
        if (b.name or "").strip().lower() == "regency shop":
            return b.id
    for b in businesses:
        n = (b.name or "").strip().lower()
        if "regency" in n and "shop" in n:
            return b.id
    return None


def _resolve_home_garden_business(businesses: list[Business]) -> int | None:
    """Pick the business that should receive home/garden leads."""
    if not businesses:
        return None
    if settings.home_garden_business_id is not None:
        bid = settings.home_garden_business_id
        if any(b.id == bid for b in businesses):
            return bid
        LOGGER.warning("HOME_GARDEN_BUSINESS_ID=%s is not enabled or missing; using heuristic.", bid)
    rs = _resolve_regency_shop_id(businesses)
    if rs is not None:
        return rs
    # Score enabled businesses by home/garden affinity in name / nature / keywords
    affinity_terms = (
        "garden",
        "home",
        "landscap",
        "lawn",
        "patio",
        "outdoor",
        "yard",
        "deck",
        "plants",
        "irrigation",
    )
    best_id: int | None = None
    best_score = -1.0
    for b in businesses:
        blob = f"{b.name} {b.nature_of_business or ''} {b.keywords or ''}".lower()
        score = 0.0
        for t in affinity_terms:
            if t in blob:
                score += 0.12
        if "garden" in blob:
            score += 0.35
        if "home" in blob and "garden" in blob:
            score += 0.5
        if score > best_score:
            best_score = score
            best_id = b.id
    if best_score >= 0.35:
        return best_id
    if len(businesses) == 1:
        return businesses[0].id
    if best_id is not None and best_score > 0:
        return best_id
    return None


def _is_tv_station(outlet: str | None) -> bool:
    """True if the outlet appears to be a TV station (we allow product/gift requests only for TV)."""
    if not (outlet or "").strip():
        return False
    lower = outlet.lower()
    tv_indicators = (
        " tv" in lower
        or "tv " in lower
        or lower.startswith("tv ")
        or lower.endswith(" tv")
        or "television" in lower
        or "channel " in lower
        or " news" in lower
        or "nbc" in lower
        or "cbs" in lower
        or "abc " in lower
        or " fox" in lower
        or "cnn" in lower
        or "msnbc" in lower
        or "affiliate" in lower
    )
    return bool(tv_indicators)


def _business_catalog_terms(b: Business) -> list[str]:
    terms: list[str] = []
    for k in (b.keywords or "").split(","):
        k = k.strip().lower()
        if k:
            terms.append(k)
    for blob in (b.nature_of_business or "", b.name or ""):
        for w in re.findall(r"[a-z]{3,}", blob.lower()):
            if w not in {"the", "and", "for", "with", "all", "our", "your", "shop", "store"}:
                terms.append(w)
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


_FOUNDER_EXPERT_RE = re.compile(
    r"\b("
    r"brand founders?|business owners?|company founders?|co-?founders?|"
    r"entrepreneurs?|small business owners?|startup founders?|"
    r"e-?commerce founders?|industry experts?|"
    r"swimwear designers?|fashion designers?|"
    r"designers?\s*/\s*brand founders?|"
    r"expert advice|expert insight|expert quote"
    r")\b",
    re.I,
)


def _is_founder_expert_query(text: str) -> bool:
    if not (text or "").strip():
        return False
    lower = text.lower()
    if _FOUNDER_EXPERT_RE.search(text):
        return True
    if re.search(r"\bfounders?\b", lower) and re.search(
        r"\b(designer|expert|advice|quote|insight|perspective|commentary|entrepreneur)\b", lower
    ):
        return True
    if "business owner" in lower or "entrepreneur" in lower:
        return True
    return False


def _vertical_overlap_score(b: Business, text: str) -> float:
    lowered = (text or "").lower()
    if not lowered:
        return 0.0
    score = 0.0
    for term in _business_catalog_terms(b):
        if len(term) < 3:
            continue
        if " " in term or "-" in term:
            if term in lowered:
                score += 0.18
        elif re.search(r"\b" + re.escape(term) + r"\b", lowered):
            score += 0.14
    return min(1.0, score)


def _audit_from_vertical_scores(
    scores: dict[int, float],
    enabled: list[Business],
    source: str,
    reason_fmt: str,
    min_relevant: float = 0.2,
) -> list[dict]:
    out = []
    for b in enabled:
        s = scores.get(b.id, 0.0)
        rel = s >= min_relevant
        out.append(
            {
                "business_id": b.id,
                "name": (b.name or "").strip() or ("Business %s" % b.id),
                "relevant": rel,
                "reason": reason_fmt % s if rel else "Vertical overlap score %.2f — below threshold." % s,
                "source": source,
            }
        )
    return out


def _try_founder_expert_match(
    request: HaroRequest, enabled: list[Business], hv: bool, hc: float
) -> MatchResult | None:
    """Strong keyword + founder/expert ask → match the best-fit business without home bias."""
    text = request.request_text or ""
    if not _is_founder_expert_query(text):
        return None
    scores = {b.id: _vertical_overlap_score(b, text) for b in enabled}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    min_overlap = 0.14 if _is_founder_expert_query(text) else 0.2
    if not ranked or ranked[0][1] < min_overlap:
        return None
    best_id, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < 0.24 and second >= best_score - 0.04:
        return None
    audit = _audit_from_vertical_scores(
        scores,
        enabled,
        "founder_expert",
        "Founder/expert query aligns with this business (overlap %.2f).",
        min_relevant=min_overlap,
    )
    return MatchResult(
        True,
        best_id,
        min(0.92, 0.72 + best_score * 0.25),
        "Founder/expert query matches business vertical (founder-expert path).",
        ["founder_expert"],
        hv,
        hc,
        audit,
    )


def _apply_founder_expert_audit_patch(
    request: HaroRequest, audit: list[dict], enabled: list[Business]
) -> list[dict]:
    if not _is_founder_expert_query(request.request_text or ""):
        return audit
    by_id = {b.id: b for b in enabled}
    patched = []
    for row in audit:
        bid = row.get("business_id")
        b = by_id.get(bid) if bid is not None else None
        if b is None:
            patched.append(row)
            continue
        overlap = _vertical_overlap_score(b, request.request_text or "")
        min_overlap = 0.14 if _is_founder_expert_query(request.request_text or "") else 0.2
        if overlap >= min_overlap and not row.get("relevant"):
            patched.append(
                {
                    **row,
                    "relevant": True,
                    "reason": (
                        "Founder/expert query + business vertical overlap (%.2f); marked relevant."
                        % overlap
                    )[:500],
                    "source": "founder_expert_patch",
                }
            )
        else:
            patched.append(row)
    return patched


def _reconcile_ai_decision(
    ai_result: dict,
    audit: list[dict],
    heuristic_scores: dict[int, float],
    enabled: list[Business],
) -> tuple[bool, int | None, float, str]:
    """Prefer per-business audit + heuristics over a wrong global matched=false."""
    ai_matched = bool(ai_result.get("matched"))
    ai_id = ai_result.get("matched_business_id")
    try:
        ai_id = int(ai_id) if ai_id is not None else None
    except (TypeError, ValueError):
        ai_id = None
    ai_conf = float(ai_result.get("confidence", 0.0))
    reasoning = (ai_result.get("reasoning_short") or "Hybrid classification decision.")[:240]

    relevant_ids = [int(r["business_id"]) for r in audit if r.get("relevant") and r.get("business_id") is not None]

    def _blend(bid: int) -> float:
        h = heuristic_scores.get(bid, 0.0)
        return min(1.0, (ai_conf + h) / 2 if ai_matched and bid == ai_id else max(ai_conf * 0.55, h) + 0.12)

    if ai_matched and ai_id is not None and ai_id in relevant_ids:
        return True, ai_id, _blend(ai_id), reasoning

    if ai_matched and ai_id is not None and relevant_ids and ai_id not in relevant_ids:
        best = max(relevant_ids, key=lambda i: heuristic_scores.get(i, 0.0))
        return True, best, _blend(best), "Audit override: matched business not relevant; using best audit fit."

    if relevant_ids:
        best = max(relevant_ids, key=lambda i: heuristic_scores.get(i, 0.0))
        if heuristic_scores.get(best, 0.0) >= 0.12 or len(relevant_ids) == 1:
            return (
                True,
                best,
                _blend(best),
                reasoning or "Matched from per-business audit (multi-vertical).",
            )

    if ai_matched and ai_id is not None:
        blended = _blend(ai_id)
        if blended >= 0.35:
            return True, ai_id, blended, reasoning

    return False, None, ai_conf, reasoning


def _classifier_system_prompt() -> str:
    return (
        "You classify HARO journalist queries against a MULTI-BUSINESS portfolio. "
        "Each business has its own niche (nature_of_business and keywords). "
        "Evaluate EVERY business independently — do NOT apply one business's niche to another. "
        "CRITICAL: When a query seeks founders, brand founders, business owners, entrepreneurs, "
        "designers, or industry experts, mark a business RELEVANT if its nature_of_business and keywords "
        "align with the query topic — the contact can respond as the founder/expert even when the query "
        "does not mention 'home' or 'lifestyle'. "
        "Example: swimwear designers/brand founders → relevant for a swimwear brand; NOT relevant for a furniture store. "
        "Pick matched_business_id as the single BEST fit among businesses marked relevant. "
        "Reject only when no business could credibly answer. "
        "Global exclusions (all businesses): in-person-only participation; unrelated sectors "
        "(finance, crypto, legal SaaS, supplements, etc.) when that is the main topic. "
        "We do not send products or gifts except for TV station outlets."
    )


def _classifier_user_prompt(
    request: HaroRequest,
    hg_score_result: HomeGardenScoreResult | None,
) -> str:
    outlet_info = (
        f" Outlet for this request: {request.outlet or 'unknown'}."
        if request.outlet
        else " No outlet specified."
    )
    founder_note = ""
    if _is_founder_expert_query(request.request_text or ""):
        founder_note = (
            "\nFOUNDER/EXPERT QUERY DETECTED: reporters want a founder, owner, entrepreneur, designer, "
            "or credentialed expert to quote. Match any business whose vertical fits — do not require "
            "home/lifestyle unless that is the business's niche.\n"
        )
    hg_block = ""
    hg = hg_score_result
    if hg is not None:
        hg_block = (
            f"\nHome/garden heuristic (applies ONLY to home/furniture/garden businesses): "
            f"band={hg.decision_band}, net_score={hg.total_score:.2f}. "
            f"Signals: strong={hg.matched_strong_terms}, medium={hg.matched_medium_terms}, "
            f"negatives={hg.matched_negative_terms}. "
            "Use this hint only for businesses in home decor, furniture, interior, garden, or patio — "
            "ignore it for fashion, swimwear, events, or other non-home businesses.\n"
        )
    return (
        "Classify only the QUERY below against each business in BUSINESSES.\n"
        "Rules:\n"
        "- per_business_audit: EXACTLY one entry per business with business_id, relevant (bool), reason.\n"
        "- relevant=true when this business's founder/expert could credibly respond (including founder/expert asks).\n"
        "- matched=true only if at least one business is relevant; matched_business_id = best single fit.\n"
        "- topic_tags: optional strings (e.g. home_garden for home businesses, founder_expert for founder asks).\n"
        f"{outlet_info}{founder_note}{hg_block}"
        "Return ONLY JSON: matched, matched_business_id, confidence, reasoning_short, topic_tags, per_business_audit."
    )


def _heuristic_scores(text: str, businesses: list[Business]) -> dict[int, float]:
    scores = {b.id: _vertical_overlap_score(b, text) for b in businesses}
    if _is_founder_expert_query(text):
        for b in businesses:
            if scores[b.id] >= 0.14:
                scores[b.id] = min(1.0, scores[b.id] + 0.22)
    return scores


def _select_from_heuristic(
    scores: dict[int, float], enabled: list[Business], hv: bool, hc: float
) -> MatchResult:
    audit = _audit_from_heuristic_scores(scores, enabled)
    if not scores:
        return MatchResult(False, None, 0.0, "No businesses available.", [], hv, hc, audit)
    business_id, score = max(scores.items(), key=lambda kv: kv[1])
    if score < 0.2:
        return MatchResult(False, None, score, "No reliable keyword match.", [], hv, hc, audit)
    return MatchResult(
        True,
        business_id,
        score,
        "Keyword heuristic matched business terms.",
        [],
        hv,
        hc,
        audit,
    )


def _classify_with_openai(
    request: HaroRequest,
    businesses: list[Business],
    hg_score_result: HomeGardenScoreResult | None = None,
) -> dict | None:
    if not settings.openai_api_key:
        return None
    catalog = [
        {
            "id": b.id,
            "name": b.name,
            "nature_of_business": b.nature_of_business,
            "keywords": b.keywords,
        }
        for b in businesses
    ]
    prompt = _classifier_user_prompt(request, hg_score_result)
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": _classifier_system_prompt()},
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nBUSINESSES:\n{json.dumps(catalog)}\n\n"
                        f"QUERY (only thing to evaluate):\n{request.request_text[:4000]}"
                    ),
                },
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            return None
        # Strip markdown code block if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0].strip()
        # Find first { to last } in case there's extra text
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        LOGGER.exception("OpenAI classify failed: %s", exc)
        return None
