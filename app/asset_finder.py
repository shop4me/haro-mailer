"""Locate real candidate assets before any AI generation. TODO hooks for real libraries."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from app.asset_types import AssetCandidate, AssetPlan
from app.config import settings

if TYPE_CHECKING:
    from app.models import Business, HaroRequest

LOGGER = logging.getLogger(__name__)


def find_candidate_assets(
    request: HaroRequest,
    business: Business,
    asset_plan: AssetPlan,
) -> list[AssetCandidate]:
    """
    Search order:
    1) Business-owned lifestyle paths (env BUSINESS_LIFESTYLE_IMAGE_DIRS comma paths) — TODO wire UI
    2) Pre-approved editorial library (EDITORIAL_ASSET_LIBRARY_DIR)
    3) Designer uploads (future: table media_assets) — empty for now
    """
    _ = request
    out: list[AssetCandidate] = []

    # TODO: implement DB-backed media when available
    dirs_raw = os.getenv("BUSINESS_LIFESTYLE_IMAGE_DIRS", "").strip()
    lib_dir = os.getenv("EDITORIAL_ASSET_LIBRARY_DIR", "").strip()

    for label, root in (
        ("lifestyle", dirs_raw),
        ("editorial", lib_dir),
    ):
        if not root:
            continue
        for part in root.split(","):
            p = part.strip()
            if not p or not os.path.isdir(p):
                continue
            try:
                for name in sorted(os.listdir(p))[:20]:
                    if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        continue
                    full = os.path.join(p, name)
                    out.append(
                        AssetCandidate(
                            source_type=label,
                            is_real=True,
                            is_verified=label == "editorial",
                            path_or_url=full,
                            caption=name,
                            alt_description="",
                            geography="",
                            project_type="",
                            resolution="",
                            score_components={"path_match": 0.5},
                            total_score=0.5,
                            notes="from %s dir" % label,
                        )
                    )
            except OSError as exc:
                LOGGER.warning("asset_finder scan failed path=%s: %s", p, exc)

    if out:
        LOGGER.info("asset_finder found %s real candidate(s) for business_id=%s", len(out), business.id)
    else:
        LOGGER.info("asset_finder no real candidates business_id=%s mode=%s", business.id, asset_plan.asset_mode.value)
    return out


def business_has_asset_roots_configured() -> bool:
    return bool(
        os.getenv("BUSINESS_LIFESTYLE_IMAGE_DIRS", "").strip()
        or os.getenv("EDITORIAL_ASSET_LIBRARY_DIR", "").strip()
    )
