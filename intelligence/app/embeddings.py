"""Embedding generation and similarity utilities."""

from __future__ import annotations

import hashlib
import math
import struct
from typing import TYPE_CHECKING

from app.models import EmbeddingModelInfo

if TYPE_CHECKING:
    pass


class EmbeddingManager:
    """Manages embedding generation, similarity, and drift detection."""

    def __init__(
        self,
        provider: str = "local",
        model_id: str = "local-minilm",
        dimensions: int = 384,
    ) -> None:
        self.provider = provider
        self.model_id = model_id
        self.dimensions = dimensions

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, text: str) -> list[float]:
        """Generate an embedding vector for *text*.

        For the ``local`` provider this produces deterministic
        pseudo-embeddings derived from the SHA-256 hash of the input.
        """
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
            is_default=(self.model_id == "local-minilm"),
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
