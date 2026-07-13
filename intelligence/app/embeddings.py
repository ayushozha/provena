"""Embedding generation and similarity utilities."""

from __future__ import annotations

import hashlib
import math
import struct
from typing import TYPE_CHECKING

import httpx

from app.models import EmbeddingModelInfo

if TYPE_CHECKING:
    pass


class EmbeddingManager:
    """Manages embedding generation, similarity, and drift detection."""

    def __init__(
        self,
        provider: str = "local",
        model_id: str = "",
        dimensions: int = 384,
        base_url: str = "http://localhost:11434/v1",
        api_key: str = "",
    ) -> None:
        if provider == "openai" and not model_id.strip():
            raise ValueError("PROVENA_INTEL_EMBEDDING_MODEL must name the served embedding model")
        self.provider = provider
        self.model_id = model_id
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        if self.provider != "local" and not self.model_id.strip():
            raise ValueError("model_id must be configured for non-local embedding providers")
        # Lazily created persistent client so the per-request embedding call
        # reuses one connection pool instead of a fresh TCP/TLS handshake each time.
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, text: str) -> list[float]:
        """Generate an embedding vector for *text*.

        The ``openai`` provider produces real semantic embeddings from any
        OpenAI-compatible embeddings endpoint when configured,
        pointed by env vars. The ``local`` provider produces deterministic
        SHA-256 pseudo-embeddings with no semantic signal -- retained only
        for offline/unit tests.
        """
        if self.provider == "openai":
            return self._openai_embed(text)
        if self.provider == "local":
            return self._local_embed(text)
        raise NotImplementedError(f"Provider {self.provider!r} not supported yet")

    def generate_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts."""
        return [self.generate(t) for t in texts]

    # ------------------------------------------------------------------
    # Similarity
    # ------------------------------------------------------------------

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ------------------------------------------------------------------
    # Info / drift
    # ------------------------------------------------------------------

    def get_model_info(self) -> EmbeddingModelInfo:
        """Return metadata about the current embedding model."""
        return EmbeddingModelInfo(
            model_id=self.model_id,
            provider=self.provider,
            dimensions=self.dimensions,
            max_tokens=512,
            version="1.0",
            is_default=True,
        )

    def detect_drift(
        self,
        old_model: "EmbeddingManager",
        new_model: "EmbeddingManager",
        sample_texts: list[str],
    ) -> float:
        """Compute average cosine *distance* between two models' embeddings.

        Returns a value in ``[0, 2]``; 0 means identical embeddings.
        """
        if not sample_texts:
            return 0.0
        total_distance = 0.0
        for text in sample_texts:
            vec_old = old_model.generate(text)
            vec_new = new_model.generate(text)
            sim = self.cosine_similarity(vec_old, vec_new)
            total_distance += 1.0 - sim
        return total_distance / len(sample_texts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _openai_embed(self, text: str) -> list[float]:
        """Real semantic embedding from any OpenAI-compatible endpoint.

        Pointed entirely by env: ``PROVENA_INTEL_EMBEDDING_BASE_URL`` /
        ``_MODEL`` / ``_API_KEY``. Works against a local Ollama ``/v1``
        shim, a local llama-server, OpenRouter, or OpenAI unchanged.

        Synchronous on purpose: the read/write pipelines call ``generate``
        synchronously. A failure here raises rather than silently falling
        back to pseudo-embeddings -- a down embedder must surface loudly,
        not quietly poison recall with hash noise.
        """
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        try:
            response = self._client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model_id, "input": text},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json().get("data") or []
            vector = data[0].get("embedding") if data else None
        except Exception as exc:
            raise RuntimeError(
                f"Embedding request to {self.base_url} failed for model "
                f"{self.model_id!r}: {exc}"
            ) from exc
        if not vector:
            raise RuntimeError(
                f"Embedding endpoint returned no vector for model {self.model_id!r}"
            )
        return [float(value) for value in vector]

    def _local_embed(self, text: str) -> list[float]:
        """Deterministic pseudo-embedding from SHA-256 hash bytes.

        The hash is expanded by iteratively hashing to fill the
        requested *dimensions*, then normalised to a unit vector.
        """
        raw: list[float] = []
        seed = text.encode("utf-8")
        counter = 0
        while len(raw) < self.dimensions:
            h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            # Each SHA-256 gives 32 bytes → 8 floats (4 bytes each)
            for i in range(0, 32, 4):
                if len(raw) >= self.dimensions:
                    break
                # Interpret 4 bytes as an unsigned 32-bit int, map to [-1, 1]
                (val,) = struct.unpack(">I", h[i : i + 4])
                raw.append((val / 2_147_483_647.5) - 1.0)
            counter += 1

        # Normalise to unit vector
        norm = math.sqrt(sum(x * x for x in raw))
        if norm > 0:
            raw = [x / norm for x in raw]
        return raw
