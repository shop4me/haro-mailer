"""Rank asset candidates with debuggable component scores."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from app.asset_types import AssetCandidate, AssetPlan
from app.asset_types import AssetMode

if TYPE_CHECKING:
    pass

LOGGER = logging.getLogger(__name__)


def rank_and_select_assets(
    candidates: list[AssetCandidate],
    asset_plan: AssetPlan,
    max_send: int = 3,
) -> list[AssetCandidate]:
    if not candidates:
        return []
    scored: list[AssetCandidate] = []
    for c in candidates:
        comp = dict(c.score_components)
        # Heuristic boosts
        comp["has_path"] = 1.0 if c.path_or_url and os.path.isfile(c.path_or_url) else 0.2
        comp["real_bonus"] = 0.4 if c.is_real else 0.0
        comp["verified_bonus"] = 0.3 if c.is_verified else 0.0
        if asset_plan.asset_mode == AssetMode.real_only and not c.is_real:
            comp["mode_penalty"] = -1.0
        else:
            comp["mode_penalty"] = 0.0
        total = sum(comp.values())
        scored.append(
            AssetCandidate(
                source_type=c.source_type,
                is_real=c.is_real,
                is_verified=c.is_verified,
                path_or_url=c.path_or_url,
                caption=c.caption,
                alt_description=c.alt_description,
                geography=c.geography,
                project_type=c.project_type,
                resolution=c.resolution,
                score_components=comp,
                total_score=total,
                notes=c.notes,
            )
        )
    scored.sort(key=lambda x: x.total_score, reverse=True)
    chosen = scored[: max_send]
    LOGGER.info(
        "asset_ranker selected=%s top_score=%s",
        len(chosen),
        chosen[0].total_score if chosen else None,
    )
    return chosen
