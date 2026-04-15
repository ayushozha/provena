"""Model routing — selects the cheapest model that satisfies task + tier."""

from __future__ import annotations

from app.models import ModelConfig, ModelTier


# Ordered list so adjacent-tier fallback is easy:
_TIER_ORDER: list[ModelTier] = [ModelTier.FAST, ModelTier.BALANCED, ModelTier.QUALITY]


class ModelRouter:
    """Route a task to the best-fit model from a built-in registry."""

    def __init__(self) -> None:
        self.registry: list[ModelConfig] = [
            ModelConfig(
                model_id="local-classify",
                provider="local",
                tier=ModelTier.FAST,
                max_tokens=512,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                capabilities=["classify"],
            ),
            ModelConfig(
                model_id="local-embed",
                provider="local",
                tier=ModelTier.FAST,
                max_tokens=512,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                capabilities=["embed"],
            ),
            ModelConfig(
                model_id="balanced-rerank",
                provider="local",
                tier=ModelTier.BALANCED,
                max_tokens=4096,
                cost_per_1k_input=0.001,
                cost_per_1k_output=0.001,
                capabilities=["rerank", "classify"],
            ),
            ModelConfig(
                model_id="quality-compact",
                provider="local",
                tier=ModelTier.QUALITY,
                max_tokens=8192,
                cost_per_1k_input=0.003,
                cost_per_1k_output=0.015,
                capabilities=["compact", "summarize", "classify", "rerank"],
            ),
            ModelConfig(
                model_id="haiku",
                provider="anthropic",
                tier=ModelTier.FAST,
                max_tokens=4096,
                cost_per_1k_input=0.0008,
                cost_per_1k_output=0.004,
                capabilities=["classify", "summarize"],
            ),
            ModelConfig(
                model_id="sonnet",
                provider="anthropic",
                tier=ModelTier.BALANCED,
                max_tokens=8192,
                cost_per_1k_input=0.003,
                cost_per_1k_output=0.015,
                capabilities=["classify", "summarize", "compact", "rerank"],
            ),
            ModelConfig(
                model_id="opus",
                provider="anthropic",
                tier=ModelTier.QUALITY,
                max_tokens=16384,
                cost_per_1k_input=0.015,
                cost_per_1k_output=0.075,
                capabilities=["compact", "summarize", "classify", "rerank"],
            ),
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(
        self,
        task: str,
        tier: ModelTier = ModelTier.BALANCED,
        input_tokens: int = 0,
    ) -> ModelConfig:
        """Select the cheapest model that supports *task* at *tier*.

        If no model matches the exact tier, the router falls back to
        adjacent tiers (first higher, then lower).
        """
        capable = [m for m in self.registry if task in m.capabilities]
        if not capable:
            raise ValueError(f"No model in registry supports task {task!r}")

        # Try exact tier first, then adjacent tiers outward
        for search_tier in self._tier_fallback_order(tier):
            candidates = [m for m in capable if m.tier == search_tier]
            if candidates:
                candidates.sort(key=lambda m: m.cost_per_1k_input)
                return candidates[0]

        # Should not happen if registry is non-empty, but just in case
        capable.sort(key=lambda m: m.cost_per_1k_input)
        return capable[0]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tier_fallback_order(preferred: ModelTier) -> list[ModelTier]:
        """Return tiers in fallback order: preferred, then adjacent."""
        idx = _TIER_ORDER.index(preferred)
        order = [preferred]
        left, right = idx - 1, idx + 1
        while left >= 0 or right < len(_TIER_ORDER):
            if right < len(_TIER_ORDER):
                order.append(_TIER_ORDER[right])
                right += 1
            if left >= 0:
                order.append(_TIER_ORDER[left])
                left -= 1
        return order
