"""Shared types for asset planning, candidates, and reply context."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AssetMode(str, Enum):
    no_visuals = "no_visuals"
    real_only = "real_only"
    concept_allowed = "concept_allowed"


@dataclass
class AssetPlan:
    needs_images: bool
    images_required_in_first_reply: bool
    wants_comments: bool
    requires_real_projects: bool
    requires_real_client_work: bool
    requires_geographic_verification: bool
    allowed_geography: str
    requests_original_photography: bool
    ai_risk_level: str  # low | medium | high
    asset_mode: AssetMode
    num_images_target: int
    manual_review_required: bool
    manual_review_reason: str
    visual_brief: dict[str, Any]


@dataclass
class AssetCandidate:
    source_type: str  # lifestyle_library | editorial | designer | ai_generated | stub
    is_real: bool
    is_verified: bool
    path_or_url: str
    caption: str
    alt_description: str
    geography: str
    project_type: str
    resolution: str
    score_components: dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0
    notes: str = ""


@dataclass
class AssetContext:
    """Passed into drafter and send guard."""

    asset_mode: AssetMode
    selected_assets: list[AssetCandidate]
    inline_preview_assets: list[AssetCandidate]
    full_res_link: str
    must_disclose_ai: bool
    usage_note: str
    wants_comments: bool
    images_required_in_first_reply: bool
