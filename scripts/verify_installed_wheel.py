"""Verify a noneditable wheel from an isolated interpreter, outside the checkout."""

from pathlib import Path
from tempfile import TemporaryDirectory

from app.models import MemoryCreate, ScopeEnvelope, SearchRequest
from app.store import ProvenaStore, SCHEMA_PATH
from app.store_postgres import POSTGRES_SCHEMA_PATH


def main() -> None:
    checkout = Path(__file__).resolve().parent.parent
    assert not SCHEMA_PATH.resolve().is_relative_to(checkout), "source checkout masked wheel resources"
    assert SCHEMA_PATH.is_file(), "installed SQLite schema missing"
    assert POSTGRES_SCHEMA_PATH.is_file(), "installed PostgreSQL schema missing"
    for name in ("002_tenant_integrity_sqlite.sql", "002_tenant_integrity_postgres.sql"):
        assert (SCHEMA_PATH.parent / name).is_file(), f"installed migration missing: {name}"
    with TemporaryDirectory(prefix="provena-wheel-") as directory:
        database = Path(directory) / "memory.db"
        scope = ScopeEnvelope(tenant_id="wheel-smoke")
        store = ProvenaStore(database)
        written = store.create_memory(
            MemoryCreate(kind="fact", content="installed wheel preserves cited memory", scope=scope)
        )
        store.close()
        reopened = ProvenaStore(database)
        try:
            recalled = reopened.search_memories(SearchRequest(query="cited memory", scope=scope))
            assert recalled.results[0].memory.memory_id == written.memory.memory_id
        finally:
            reopened.close()
    print("installed-wheel-persistence-recall: ok")


if __name__ == "__main__":
    main()
