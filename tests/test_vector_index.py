"""Tests for the sqlite-vec KNN fast path.

These exercise the store-level vector candidate retrieval directly so they
target the index rather than the full search scorer. They skip automatically
when the sqlite-vec extension can't be loaded (the store then uses the linear
scan, which the API-level search tests already cover).
"""

import tempfile
import unittest
from pathlib import Path

from app.models import MemoryCreate, MemoryKind, ScopeEnvelope, SearchRequest
from app.store import AccessContext, ProvenaStore

DIM = 8


def emb(*vals: float) -> list[float]:
    padded = list(vals) + [0.0] * (DIM - len(vals))
    return padded[:DIM]


class VectorIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = ProvenaStore(self.dir / "vec.db", vector_dimensions=DIM)
        self.access = AccessContext(tenant_id="t", role="admin", principal_id="p1")
        if not self.store.vec_enabled:
            self.skipTest("sqlite-vec extension not loadable in this environment")

    def tearDown(self) -> None:
        self.store.close()

    def _create(self, content: str, embedding: list[float]) -> str:
        result = self.store.create_memory(
            MemoryCreate(
                kind=MemoryKind.FACT,
                scope=ScopeEnvelope(tenant_id="t", workspace_id="ws"),
                content=content,
                embedding=embedding,
            ),
            access=self.access,
        )
        return result.memory.memory_id

    def _query(self, vector: list[float]) -> list[str]:
        req = SearchRequest(
            query="ignored",
            scope=ScopeEnvelope(tenant_id="t", workspace_id="ws"),
            query_embedding=vector,
            limit=5,
        )
        return [c.row["memory_id"] for c in self.store._vector_candidate_rows(req, candidate_limit=5)]

    def test_knn_returns_nearest_by_vector(self) -> None:
        a = self._create("alpha", emb(1, 0, 0))
        self._create("bravo", emb(0, 1, 0))
        c = self._create("charlie", emb(0.9, 0.1, 0))

        ids = self._query(emb(1, 0, 0))
        self.assertEqual(ids[0], a, "exact vector match should rank first")
        self.assertIn(c, ids, "near vector match should be retrieved")

    def test_hard_delete_evicts_from_index(self) -> None:
        a = self._create("alpha", emb(1, 0, 0))
        self.assertIn(a, self._query(emb(1, 0, 0)))
        self.store.delete_memory(a, hard_delete=True, access=self.access)
        self.assertNotIn(a, self._query(emb(1, 0, 0)))

    def test_other_tenant_is_not_returned(self) -> None:
        a = self._create("alpha", emb(1, 0, 0))
        self.store.create_memory(
            MemoryCreate(
                kind=MemoryKind.FACT,
                scope=ScopeEnvelope(tenant_id="other", workspace_id="ws"),
                content="alien",
                embedding=emb(1, 0, 0),
            ),
            access=AccessContext(tenant_id="other", role="admin", principal_id="p2"),
        )
        ids = self._query(emb(1, 0, 0))
        self.assertEqual(ids, [a], "KNN must post-filter to the requesting tenant")


if __name__ == "__main__":
    unittest.main()
