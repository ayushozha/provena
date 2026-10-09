"""Shared OpenAI-compatible LLM client for the intelligence stages.

One place that knows how to call a chat model and get JSON back, so every
stage (rerank, contradiction detection, compaction) routes through the same
path. The ``ModelRouter`` picks the provider + real served model for the
task/tier and supplies its endpoint + key; this client just dispatches the
OpenAI-compatible call. Works against Ollama, llama-server, OpenRouter, OpenAI,
or any OpenAI-compatible endpoint.

The client is best-effort: when nothing is configured (``enabled`` is False),
the router has no model for the task, or any call/parse fails, ``chat_json``
returns ``None`` and the caller falls back to its deterministic heuristic. No
stage hard-depends on an LLM being present.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from app.model_router import ModelRouter
from app.models import ModelTier

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", re.DOTALL)


class LLMClient:
    def __init__(self, model_router: ModelRouter) -> None:
        self.model_router = model_router

    @property
    def enabled(self) -> bool:
        # True when the router has at least one configured provider/model.
        return self.model_router.enabled

    async def chat_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        tier: ModelTier = ModelTier.BALANCED,
        max_tokens: int = 2048,
        response_max_bytes: int | None = None,
    ) -> Any | None:
        """OpenAI-compatible chat call returning parsed JSON, or None on any
        failure. The router selects the provider + real served model for
        *task*/*tier*; None routing (nothing configured for the task) returns
        None so the caller degrades to its heuristic."""
        routed = self.model_router.route(task, tier)
        if routed is None:
            return None
        headers = {"content-type": "application/json"}
        if routed.api_key:
            headers["Authorization"] = f"Bearer {routed.api_key}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                payload = {
                    "model": routed.model,
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                url = f"{routed.base_url.rstrip('/')}/chat/completions"
                if response_max_bytes is None:
                    response = await client.post(url, headers=headers, json=payload)
                    if response.status_code >= 400:
                        return None
                    data = response.json()
                else:
                    # Refuse compressed replies before reading rather than
                    # materializing a decompression bomb. The total deadline
                    # also bounds drip-fed replies; legacy callers are unchanged.
                    if response_max_bytes < 1:
                        return None
                    headers["Accept-Encoding"] = "identity"
                    async with asyncio.timeout(30.0):
                        async with client.stream("POST", url, headers=headers, json=payload) as response:
                            if not 200 <= response.status_code < 300 or response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                                return None
                            chunks: list[bytes] = []
                            size = 0
                            async for chunk in response.aiter_raw():
                                size += len(chunk)
                                if size > response_max_bytes:
                                    return None
                                chunks.append(chunk)
                            data = json.loads(b"".join(chunks).decode("utf-8"))
                # `or default` so an explicit null falls back to a safe type.
                choices = data.get("choices") or []
                message = (choices[0].get("message") or {}) if choices else {}
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
