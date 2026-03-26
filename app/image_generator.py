"""AI concept image generation — abstraction + stub. Never labels output as real."""

from __future__ import annotations

import logging
from typing import Protocol

from app.asset_types import AssetCandidate, AssetPlan
from app.config import settings

LOGGER = logging.getLogger(__name__)


class ImageGenerationProvider(Protocol):
    def generate(self, prompt: str, n: int) -> list[tuple[bytes, str]]:
        """Return (image_bytes, mime_subtype) per image."""


class StubImageGenerationProvider:
    """Placeholder until OpenAI Images API or another vendor is wired."""

    def generate(self, prompt: str, n: int) -> list[tuple[bytes, str]]:
        LOGGER.info("StubImageGenerationProvider.generate n=%s prompt_len=%s (no images produced)", n, len(prompt))
        return []


def _build_generation_prompt(asset_plan: AssetPlan) -> str:
    b = asset_plan.visual_brief or {}
    parts = [
        "Ultra photorealistic editorial interior photograph.",
        "Realistic natural daylight, believable materials, magazine composition.",
        "Subtle imperfections, no CGI look, no surreal geometry, no warped objects.",
        "Style: %s. Room: %s. Mood: %s." % (b.get("style", ""), b.get("room_type", ""), b.get("mood", "")),
        "Avoid: %s" % b.get("avoid_terms", "cgi, glossy fake rendering"),
    ]
    return "\n".join(parts)


def get_provider() -> ImageGenerationProvider:
    # TODO: if os.getenv("IMAGE_GEN_PROVIDER") == "openai": return OpenAIImagesProvider()
    return StubImageGenerationProvider()


def generate_candidate_images(
    asset_plan: AssetPlan,
    business,
    count: int | None = None,
) -> list[AssetCandidate]:
    """Only for concept_allowed. Returns ai_generated candidates; never is_real."""
    _ = business
    if not settings.enable_ai_concept_visuals:
        return []
    n = count if count is not None else min(settings.max_generated_candidates, 6)
    prompt = _build_generation_prompt(asset_plan)
    provider = get_provider()
    raw_list = provider.generate(prompt, n)
    out: list[AssetCandidate] = []
    for i, (data, sub) in enumerate(raw_list):
        out.append(
            AssetCandidate(
                source_type="ai_generated",
                is_real=False,
                is_verified=False,
                path_or_url="inline_bytes:%s" % i,
                caption="Concept visual %s" % (i + 1),
                alt_description="AI-generated styling concept, not a real client project.",
                geography="",
                project_type="concept",
                resolution="generated",
                score_components={"stub": 0.0},
                total_score=0.0,
                notes="bytes_len=%s" % len(data),
            )
        )
    return out
