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


def _apply_regency_niche_gate_result(
    request: HaroRequest,
    result: MatchResult,
    enabled: list[Business],
    inbound_source: str | None,
) -> MatchResult:
    m, bid, reason, tags = apply_regency_niche_gate_to_match(
        request,
        result.matched,
        result.matched_business_id,
        enabled,
        inbound_source,
        result.reasoning_short,
        result.topic_tags,
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
        )
    return MatchResult(
        m,
        bid,
        result.confidence,
        reason,
        tags,
        result.requires_visuals,
        result.visual_request_confidence,
    )


def classify_request(
    request: HaroRequest,
    businesses: list[Business],
    inbound_source: str | None = None,
) -> MatchResult:
    hv, hc = _visual_request_hints_from_text(request.request_text or "")
    enabled = [b for b in businesses if b.enabled]
    if not enabled:
        return MatchResult(False, None, 0.0, "No enabled businesses configured.", [], hv, hc)

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
        )

    hg = _score_home_and_garden_topic(request)
    LOGGER.info("Home/garden heuristic: %s", hg.log_summary())
    if hg.notes:
        LOGGER.info("Home/garden scorer notes: %s", hg.notes)

    # Policy: only immediate home_garden tag when heuristic is STRONG (not brittle keyword-only).
    if _is_clear_home_and_garden_match(hg):
        hg_business_id = _resolve_home_garden_business(enabled)
        if hg_business_id is not None:
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
                ),
                enabled,
                inbound_source,
            )
        LOGGER.warning(
            "Home/garden strong band but no business resolved; set HOME_GARDEN_BUSINESS_ID or tune businesses. %s",
            hg.log_summary(),
        )

    heuristic_scores = _heuristic_scores(request.request_text, enabled)
    ai_result = _classify_with_openai(request, enabled, hg_score_result=hg)

    if not ai_result:
        return _apply_regency_niche_gate_result(
            request, _select_from_heuristic(heuristic_scores, hv, hc), enabled, inbound_source
        )

    best_h_id, best_h_score = max(heuristic_scores.items(), key=lambda kv: kv[1], default=(None, 0.0))
    chosen_id = ai_result.get("matched_business_id")
    ai_conf = float(ai_result.get("confidence", 0.0))
    if chosen_id in heuristic_scores:
        blended = (ai_conf + heuristic_scores[chosen_id]) / 2
    else:
        blended = ai_conf * 0.7 + best_h_score * 0.3
        if blended < 0.45 and best_h_id is not None:
            chosen_id = best_h_id

    matched = bool(ai_result.get("matched")) and chosen_id is not None
    if blended < 0.35:
        matched = False
        chosen_id = None

    tags = ai_result.get("topic_tags") or []
    if not isinstance(tags, list):
        tags = []
    # Borderline / broad home: AI may confirm home_garden for draft-only behavior downstream.
    tags = [str(t) for t in tags if t is not None]
    reasoning = (ai_result.get("reasoning_short") or "Hybrid classification decision.")[:240]
    return _apply_regency_niche_gate_result(
        request,
        MatchResult(matched, chosen_id, max(0.0, min(1.0, blended)), reasoning, tags, hv, hc),
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


def _heuristic_scores(text: str, businesses: list[Business]) -> dict[int, float]:
    lowered = text.lower()
    scores: dict[int, float] = {}
    for b in businesses:
        score = 0.0
        keys = [k.strip().lower() for k in (b.keywords or "").split(",") if k.strip()]
        for key in keys:
            if key in lowered:
                score += 0.12
        if (b.nature_of_business or "").lower() in lowered:
            score += 0.2
        scores[b.id] = min(score, 1.0)
    return scores


def _select_from_heuristic(
    scores: dict[int, float], hv: bool, hc: float
) -> MatchResult:
    if not scores:
        return MatchResult(False, None, 0.0, "No businesses available.", [], hv, hc)
    business_id, score = max(scores.items(), key=lambda kv: kv[1])
    if score < 0.2:
        return MatchResult(False, None, score, "No reliable keyword match.", [], hv, hc)
    return MatchResult(
        True, business_id, score, "Keyword heuristic matched business terms.", [], hv, hc
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
    outlet_info = f" Outlet for this request: {request.outlet or 'unknown'}." if request.outlet else " No outlet specified."

    hg = hg_score_result
    hg_block = ""
    if hg is not None:
        hg_block = (
            f"\nHome/garden heuristic (pre-score): band={hg.decision_band}, net_score={hg.total_score:.2f}. "
            f"Matched signals: strong={hg.matched_strong_terms}, medium={hg.matched_medium_terms}, "
            f"weak={hg.matched_weak_terms}, negatives={hg.matched_negative_terms}.\n"
        )
        if hg.decision_band == "borderline":
            hg_block += (
                "This query is BORDERLINE for home/lifestyle: prefer inclusion if a credible quote or expertise "
                "angle exists for home decor, furniture, interior design, remodeling, organization, cleaning, "
                "seasonal home, garden, patio, or homeowner lifestyle. Do not force a match for unrelated sectors.\n"
            )
        elif hg.decision_band == "clear_non_match":
            hg_block += (
                "Heuristic suggests this is likely NOT a home/garden lifestyle query unless the query text clearly "
                "implies decor, design, remodeling, spaces, garden, or similar—then match.\n"
            )

    prompt = (
        "You are classifying only the QUERY below (the reporter's actual request). "
        "Decide: can any of the given businesses reply to this query? Only match if the query is relevant to that business.\n"
        "Domain breadth: this operation prioritizes broad but relevant HOME and LIFESTYLE topics — including home decor, "
        "furniture, interior design, space planning, remodeling, renovation, cleaning, organization, storage, seasonal home, "
        "outdoor living, patio, backyard, garden, and homeowner lifestyle angles. Favor inclusion when a plausible expert "
        "quote or practical homeowner angle exists.\n"
        "Exclusions: reject obvious junk unrelated to home living even if the word 'home' appears — e.g. meal kits, "
        "finance/mortgages/insurance as the main topic, crypto, legal/business software, recruiting, supplements, "
        "automotive, pure tech/SaaS, gambling, etc.\n"
        "Rules: We never appear in person (do not match if the query requires in-person, studio, video/phone interview, or event attendance). "
        "We do not send products or gifts EXCEPT when the outlet is a TV station—then product/gift requests are allowed."
        f"{outlet_info}\n"
        f"{hg_block}"
        "If you match primarily because of home decor, furniture, interior design, remodeling, garden, patio, cleaning, "
        "organization, or similar homeowner expertise, include the string 'home_garden' in topic_tags (along with any other tags). "
        "Return ONLY a single JSON object, no other text or markdown. Keys: matched (bool), matched_business_id (int or null), "
        "confidence (0-1), reasoning_short (string), topic_tags (array of strings)."
    )
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a HARO classifier balancing recall and precision for HOME and LIFESTYLE relevance. "
                        "Only the query matters for the decision. Match when an expert could credibly answer from home "
                        "decor, furniture, design, remodeling, organization, cleaning, garden, patio, or homeowner life. "
                        "Reject finance, insurance-as-main-topic, meal kits, crypto, unrelated SaaS, legal industry, "
                        "automotive, supplements, and similar. We do not appear in person. We do not send products or gifts except for TV stations."
                    ),
                },
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
