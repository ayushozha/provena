"""Shared OpenAI-compatible LLM client for the intelligence stages.

One place that knows how to call a chat model and get JSON back, so every
stage (fact extraction, rerank, contradiction detection, compaction) routes
through the same provider-agnostic path. Works against Ollama, llama-server,
OpenRouter, OpenAI, or an Anthropic OpenAI-compat shim — selected entirely via
``settings.llm_base_url`` / ``llm_api_key`` / ``llm_model``.

The client is best-effort: when no key is configured (``enabled`` is False) or
any call/parse fails, ``chat_json`` returns ``None`` and the caller falls back
to its deterministic heuristic. No stage hard-depends on an LLM being present.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.model_router import ModelRouter
from app.models import ModelTier

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", re.DOTALL)


class LLMClient:
    def __init__(
        self,
        model_router: ModelRouter,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "",
        model: str = "",
    ) -> None:
        self.model_router = model_router
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Empty => use whatever model the router selects for the task.
        self.model = model

    @property
    def enabled(self) -> bool:
        # Local servers accept any bearer token, so set a dummy key to use one.
        return bool(self.api_key)

    async def chat_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        tier: ModelTier = ModelTier.BALANCED,
        max_tokens: int = 2048,
    ) -> Any | None:
        """OpenAI-compatible chat call returning parsed JSON, or None on any
        failure/disabled. The router selects the model for *task*/*tier*; an
        explicit configured model overrides it."""
        if not self.enabled:
            return None
        routed = self.model_router.route(task, tier)
        model = self.model or routed.model_id
        headers = {"content-type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": 0,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                )
                if response.status_code >= 400:
                    return None
                data = response.json()
                choices = data.get("choices", [])
                message = choices[0].get("message", {}) if choices else {}
                # Reasoning models leave content empty and put text in `reasoning`.
                raw = message.get("content") or message.get("reasoning") or ""
                return self._parse_json(raw)
        except Exception:
            return None

    @staticmethod
    def _parse_json(raw: str) -> Any | None:
        text = (raw or "").strip()
        if not text:
            return None
        fence = _FENCE.search(text)
        if fence:
            text = fence.group(1)
        else:
            # Fall back to the outermost JSON object/array in the text.
            starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
            ends = [i for i in (text.rfind("}"), text.rfind("]")) if i >= 0]
            if starts and ends:
                text = text[min(starts) : max(ends) + 1]
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None
