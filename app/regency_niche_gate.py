"""
Compatibility shim: Regency Shop gating is implemented in app.regency_ai_relevance (AI-only).
"""

from app.regency_ai_relevance import (
    apply_regency_niche_gate_to_match,
    is_regency_business,
    request_summary,
)

__all__ = [
    "apply_regency_niche_gate_to_match",
    "is_regency_business",
    "request_summary",
]
