# Autonoma Integration Notes

The local Autonoma repo already exposes a reusable Vitest integration harness in:

- `C:\Users\ayush\Desktop\OpenSource\autonoma\packages\integration-test\README.md`

Provena is intentionally structured so it can plug into that harness without
changing service code:

- Boot the service with a temporary `PROVENA_DB_PATH`.
- Seed baseline memories through the HTTP API.
- Run scenario cases against the public endpoints and SDKs.

Suggested Autonoma scenario packs for Provena:

- Memory write/read idempotency
- Scoped recall across tenant/workspace/project/user/session
- Supersession and contradiction handling
- Relation traversal and provenance citation integrity
- Soft delete, hard delete, and RTBF erase flows
- Cross-SDK parity for TS, Python, Go, and Rust

The local `scripts/run_e2e.py` runner is the immediate verification path inside
this repo. It can later be wrapped by an Autonoma `IntegrationHarness` without
changing the API contract.
