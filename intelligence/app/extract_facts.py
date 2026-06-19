"""ADD-only fact extraction — split ingest blobs into atomic memories."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx

from app.model_router import ModelRouter
from app.models import ModelTier

_ROLE_LINE = re.compile(r"^(user|assistant):\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MIN_FACT_LEN = 12


@dataclass(frozen=True)
class ExtractedFact:
    content: str
    title: str = ""
    kind: str = "fact"


class FactExtractor:
    """Mem0-style ADD extraction: accumulate atomic facts, never overwrite."""

    def __init__(
        self,
        model_router: ModelRouter,
        llm_api_key: str = "",
        llm_model: str = "",
        llm_base_url: str = "http://localhost:11434/v1",
    ) -> None:
        self.model_router = model_router
        self.llm_api_key = llm_api_key or os.environ.get("PROVENA_INTEL_LLM_API_KEY", "")
        # Empty => defer to whatever the router picks for the task.
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url.rstrip("/")

    @property
    def llm_enabled(self) -> bool:
        # Gate the LLM path on a configured key. Local servers (Ollama,
        # llama-server) accept any bearer token, so to use one set a dummy
        # PROVENA_INTEL_LLM_API_KEY alongside a localhost base_url. Without a
        # key, extraction falls back to the deterministic local splitter.
        return bool(self.llm_api_key)

    @property
    def disabled(self) -> bool:
        return os.environ.get("PROVENA_EXTRACT_DISABLED", "").lower() in {"1", "true", "yes"}

    async def extract(self, content: str, title: str = "") -> list[ExtractedFact]:
        text = content.strip()
        if not text or self.disabled:
            return [ExtractedFact(content=text or content, title=title)]

        if self.llm_enabled:
            try:
                facts = await self._llm_extract(text, title)
                if facts:
                    return facts
            except Exception:
                pass

        return self._local_extract(text, title)

    async def _llm_extract(self, content: str, title: str) -> list[ExtractedFact]:
        # The router selects the model for this task/tier; its choice is the
        # default model name, overridable by an explicit configured llm_model.
        routed = self.model_router.route("extract", ModelTier.BALANCED)
        model = self.llm_model or routed.model_id
        prompt = (
            "Extract atomic facts from the text below. ADD-only: output new facts only, "
            "never instructions to delete or replace prior knowledge. "
            "Return JSON: {\"facts\": [{\"content\": \"...\", \"kind\": \"fact\"}]}. "
            "Use third-person phrasing (User/Assistant). Max 12 facts.\n\n"
            f"TEXT:\n{content}"
        )
        headers = {"content-type": "application/json"}
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.llm_base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": 2048,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": "You extract atomic facts and reply with JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(response.text)
            data = response.json()
            choices = data.get("choices", [])
            message = choices[0].get("message", {}) if choices else {}
            # Reasoning models may leave content empty and put text in "reasoning".
            raw_text = message.get("content") or message.get("reasoning") or ""
            payload = self._parse_llm_json(raw_text)
            facts = [
                ExtractedFact(
                    content=str(item.get("content", "")).strip(),
                    title=title or str(item.get("content", ""))[:80],
                    kind=str(item.get("kind", "fact")),
                )
                for item in payload.get("facts", [])
                if str(item.get("content", "")).strip()
            ]
            return self._dedupe_facts(facts) if facts else []

    def _local_extract(self, content: str, title: str) -> list[ExtractedFact]:
        facts: list[ExtractedFact] = []
        matches = list(_ROLE_LINE.finditer(content))
        if matches:
            for match in matches:
                role, line_text = match.group(1).lower(), match.group(2).strip()
                for sentence in self._split_sentences(line_text):
                    fact_text = self._role_atomic(sentence, role)
                    if len(fact_text) >= _MIN_FACT_LEN:
                        facts.append(ExtractedFact(content=fact_text, title=title or fact_text[:80]))
        else:
            for sentence in self._split_sentences(content):
                cleaned = sentence.strip()
                if len(cleaned) >= _MIN_FACT_LEN:
                    facts.append(ExtractedFact(content=cleaned, title=title or cleaned[:80]))

        if not facts:
            return [ExtractedFact(content=content, title=title)]
        return self._dedupe_facts(facts)

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        parts = _SENTENCE_SPLIT.split(text.strip())
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _role_atomic(sentence: str, role: str) -> str:
        subject = "User" if role == "user" else "Assistant"
        lower = subject.lower()
        text = sentence.strip()
        text = re.sub(r"\bI'm\b", f"{subject} is", text, flags=re.IGNORECASE)
        text = re.sub(r"\bI've\b", f"{subject} has", text, flags=re.IGNORECASE)
        text = re.sub(r"\bI'll\b", f"{subject} will", text, flags=re.IGNORECASE)
        text = re.sub(r"\bI'd\b", f"{subject} would", text, flags=re.IGNORECASE)
        text = re.sub(r"\bI\b", subject, text)
        text = re.sub(r"\bmy\b", f"{lower}'s", text, flags=re.IGNORECASE)
        text = re.sub(r"\bme\b", lower, text, flags=re.IGNORECASE)
        if not text[0].isupper():
            text = text[0].upper() + text[1:]
        return text

    @staticmethod
    def _parse_llm_json(raw_text: str) -> dict:
        text = raw_text.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        return json.loads(text)

    @staticmethod
    def _dedupe_facts(facts: list[ExtractedFact]) -> list[ExtractedFact]:
        seen: set[str] = set()
        unique: list[ExtractedFact] = []
        for fact in facts:
            key = fact.content.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(fact)
        return unique