"""Per-model USD pricing tables for harness cost tracking.

Pricing is best-effort and should be verified against current provider docs.
Returning None for an unknown model is the explicit "skip cost enforcement"
signal — the harness driver tolerates that gracefully.

Anthropic prefix matching exists because model IDs include date suffixes
(e.g. claude-sonnet-4-5-20250929) that we don't want to re-add on every model
release. Order matters: longer prefixes win.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token pricing in USD."""

    input_per_mtok: float
    output_per_mtok: float


# Verify periodically against provider docs. Sources of truth:
#   - https://www.anthropic.com/pricing
#   - https://ai.google.dev/pricing
PRICING: Dict[str, ModelPricing] = {
    # Anthropic — prefix-matched (newest first)
    "claude-opus-4-7": ModelPricing(15.00, 75.00),
    "claude-opus-4-6": ModelPricing(15.00, 75.00),
    "claude-opus-4": ModelPricing(15.00, 75.00),
    "claude-sonnet-4-6": ModelPricing(3.00, 15.00),
    "claude-sonnet-4-5": ModelPricing(3.00, 15.00),
    "claude-sonnet-4": ModelPricing(3.00, 15.00),
    "claude-haiku-4-5": ModelPricing(1.00, 5.00),
    "claude-haiku-4": ModelPricing(1.00, 5.00),
    # Gemini — prefix-matched
    "gemini-2.5-pro": ModelPricing(1.25, 5.00),
    "gemini-2.5-flash": ModelPricing(0.075, 0.30),
    "gemini-2.0-flash": ModelPricing(0.075, 0.30),
    # Local — free
    "ollama": ModelPricing(0.0, 0.0),
}


def get_pricing(model: str) -> Optional[ModelPricing]:
    """Return pricing for a model, matching by prefix.

    Returns None if no prefix matches — caller should skip cost computation.
    """
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    # Longest prefix match so claude-opus-4-7 doesn't get caught by claude-opus-4
    for prefix in sorted(PRICING, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICING[prefix]
    return None


def compute_cost_usd(
    model: str, prompt_tokens: int, completion_tokens: int
) -> Optional[float]:
    """Compute USD cost for a single iteration's token usage.

    Returns None when pricing is unknown for the model — caller should skip
    cost enforcement rather than treat the bill as zero.
    """
    pricing = get_pricing(model)
    if pricing is None:
        return None
    input_cost = (prompt_tokens / 1_000_000) * pricing.input_per_mtok
    output_cost = (completion_tokens / 1_000_000) * pricing.output_per_mtok
    return input_cost + output_cost
