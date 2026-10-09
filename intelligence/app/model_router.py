"""Multi-provider model routing.

Routes an LLM task + tier to a concrete (provider, served-model) pair drawn from
an operator-configured registry. Every routable model is a REAL served model
name on a real OpenAI-compatible endpoint with its own ``base_url`` + ``api_key``
— there are no abstract placeholder ids, and nothing is routable unless the
operator configured a provider for it. If no configured model can serve a task,
``route`` returns ``None`` and the caller falls back to its deterministic
heuristic.

Configuration (env, prefix ``PROVENA_INTEL_``):

* Single-provider shorthand — set ``LLM_BASE_URL`` / ``LLM_API_KEY`` /
  ``LLM_MODEL``. The configured model serves every task at every tier.
* Multi-provider — set ``LLM_PROVIDERS`` to a JSON array, e.g.::

    [{"name": "openrouter",
      "base_url": "https://openrouter.ai/api/v1",
      "api_key": "sk-or-...",
      "models": [
        {"model": "<fast-model-id>", "tier": "fast",
         "tasks": ["classify", "summarize"], "cost_per_1k_input": 0.0008},
        {"model": "<balanced-model-id>", "tier": "balanced",
         "tasks": ["extract", "rerank", "compact"], "cost_per_1k_input": 0.003}]}]

  ``route`` picks the cheapest configured model that supports the task at the
  requested tier, falling back to adjacent tiers. OpenRouter (or any
  OpenAI-compatible gateway) is just one provider that itself fans out to many
  models, so a single-key deployment still gets real multi-model routing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.models import ModelTier

logger = logging.getLogger(__name__)

# Ordered low -> high so adjacent-tier fallback is symmetric.
_TIER_ORDER: list[ModelTier] = [ModelTier.FAST, ModelTier.BALANCED, ModelTier.QUALITY]

# Every LLM task the pipelines route. The single-provider shorthand serves all
# of them with the one configured model.
ALL_TASKS: frozenset[str] = frozenset({"classify", "summarize", "extract", "rerank", "compact", "abstract"})


@dataclass(frozen=True)
class RoutedModel:
    """A concrete routing decision: which endpoint + served model to call."""

    provider: str
    base_url: str
    api_key: str
    model: str  # the real served model name sent as the wire `model` field
    tier: ModelTier


@dataclass(frozen=True)
class _Entry:
    provider: str
    base_url: str
    api_key: str
    model: str
    tier: ModelTier
    tasks: frozenset[str]
    cost_per_1k_input: float


class ModelRouter:
    """Route a task + tier to a configured provider/model, cheapest-first."""

    def __init__(self, entries: list[_Entry] | None = None) -> None:
        self._entries = entries or []

    @property
    def enabled(self) -> bool:
        """True when at least one provider/model is configured."""
        return bool(self._entries)

    def route(self, task: str, tier: ModelTier = ModelTier.BALANCED) -> RoutedModel | None:
        """Cheapest configured model that supports *task* at *tier* (adjacent-tier
        fallback). Returns None when nothing is configured for the task, so the
        caller degrades to its heuristic instead of calling a doomed endpoint."""
        capable = [e for e in self._entries if task in e.tasks]
        if not capable:
            return None
        for search_tier in self._tier_fallback_order(tier):
            candidates = [e for e in capable if e.tier == search_tier]
            if candidates:
                best = min(candidates, key=lambda e: e.cost_per_1k_input)
                return self._to_routed(best)
        # capable but no tier matched (shouldn't happen) — cheapest overall.
        return self._to_routed(min(capable, key=lambda e: e.cost_per_1k_input))

    @staticmethod
    def _to_routed(entry: _Entry) -> RoutedModel:
        return RoutedModel(
            provider=entry.provider,
            base_url=entry.base_url,
            api_key=entry.api_key,
            model=entry.model,
            tier=entry.tier,
        )

    @staticmethod
    def _tier_fallback_order(preferred: ModelTier) -> list[ModelTier]:
        """Preferred tier first, then adjacent tiers outward — covers all tiers."""
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

    # ------------------------------------------------------------------
    # Construction from settings
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings) -> "ModelRouter":
        """Build a router from intelligence settings.

        ``LLM_PROVIDERS`` (JSON) takes precedence; otherwise the
        ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL`` shorthand defines one
        provider serving every task. Returns a disabled router (no entries) when
        nothing usable is configured."""
        providers_json = (getattr(settings, "llm_providers", "") or "").strip()
        if providers_json:
            return cls(cls._entries_from_json(providers_json))
        return cls(cls._entries_from_shorthand(settings))

    @staticmethod
    def _entries_from_shorthand(settings) -> list[_Entry]:
        model = (getattr(settings, "llm_model", "") or "").strip()
        if not model:
            # A key/endpoint without a model is not routable — disabled.
            return []
        api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        return [
            _Entry(
                provider="default",
                base_url=settings.llm_base_url,
                api_key=api_key,
                model=model,
                tier=ModelTier.BALANCED,  # served for every tier via fallback
                tasks=ALL_TASKS,
                cost_per_1k_input=0.0,
            )
        ]

    @staticmethod
    def _entries_from_json(raw: str) -> list[_Entry]:
        try:
            providers = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("PROVENA_INTEL_LLM_PROVIDERS is not valid JSON; LLM stages disabled.")
            return []
        entries: list[_Entry] = []
        for provider in providers if isinstance(providers, list) else []:
            if not isinstance(provider, dict):
                continue
            name = str(provider.get("name") or "")
            base_url = str(provider.get("base_url") or "")
            api_key = str(provider.get("api_key") or "")
            if not base_url:
                logger.warning("Skipping provider %r: no base_url.", name or "<unnamed>")
                continue
            models = provider.get("models")
            for spec in (models if isinstance(models, list) else []):
                if not isinstance(spec, dict):
                    continue
                model = str(spec.get("model") or "")
                if not model:
                    continue
                try:
                    tier = ModelTier(str(spec.get("tier") or "balanced"))
                except ValueError:
                    tier = ModelTier.BALANCED
                tasks = spec.get("tasks")
                if not isinstance(tasks, list):
                    tasks = list(ALL_TASKS)
                try:
                    cost = float(spec.get("cost_per_1k_input") or 0.0)
                except (ValueError, TypeError):
                    cost = 0.0
                entries.append(
                    _Entry(
                        provider=name or base_url,
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        tier=tier,
                        tasks=frozenset(str(t) for t in tasks),
                        cost_per_1k_input=cost,
                    )
                )
        return entries
