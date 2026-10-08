# Evidence-backed memory release

The user requested that the canonical Provena repository be made usable from
main, updated to current dependencies, and improved beyond procedural-memory
systems. This plan retains that full objective. A feature list or a synthetic
test does not prove superiority over another product.

## Requirements and release evidence

1. Preserve the portable append-only ledger, citations, temporal lineage,
   sensitivity checks, tenant isolation, legal holds, and erasure behavior.
2. Integrate and independently validate the existing authenticated-service and
   storage-integrity work without losing the newer repository-memory bridge.
3. Update direct dependencies and locked transitive dependencies to current
   compatible versions, remove known advisories, and declare supported runtimes.
   Major-version changes require actual build, behavior, and install evidence.
4. Make a clean checkout installable without an unpublished registry package;
   verify a packed CLI and a noneditable Python wheel in isolated directories.
5. Add structured procedural and episodic memory using the existing ledger:
   bounded tool traces, applicability, source fingerprints, verification
   receipts, failure information, and exact procedure-version references.
6. Learn from explicitly recorded outcomes, abstain for unrelated tasks, and
   reject stale or incompatible procedural guidance. Memory remains data; the
   memory layer does not execute recorded tool calls or grant permissions.
7. Expose the same procedure lifecycle through CLI, exported API, and MCP.
   Keep model choice configuration-driven and preserve the offline path.
8. Run reproducible comparisons with frozen data, independent goal checks,
   equal budgets, held-out queries, changed sources, failures, and full
   denominators. Record external competitors as unmeasured until their actual
   implementations have been legally run under equivalent conditions.
9. Complete correctness/security/test/documentation review, publish through a
   pull request, merge to main, and verify main CI and fresh-install behavior.

## Architecture

The existing workflow event carries a versioned `procedure` structured payload.
It records a goal, triggers, applicable paths, prerequisites, ordered named tool
steps with structured arguments, expected results, validation requirements,
source evidence, and recovery guidance. Learning records a candidate. Explicit
approval creates a superseding approved version; importing a successful trace
does not silently make its instructions authoritative.

Outcome events use the existing ledger contract and reference the exact
procedure event. They include task/session identity, success/failure/unknown,
verification evidence and a unique episode identity. Validation rejects missing
targets, cross-version outcome reuse, duplicates, and invalid trace payloads.
Derived reliability is evidence accounting rather than a model-generated claim.

Recall gates candidates by real query overlap or exact selectors before adding
importance, confidence, graph, and outcome priors. Procedure replay guidance
also checks source blob hashes and prerequisites. It returns a bounded cited
pointer and inspectable structured steps, with review/stale/failure states.
It never invokes arbitrary recorded commands. Episodic failures remain
available for diagnosis instead of being erased to improve reported scores.

## Independent source research

Memorable's public description identifies graph-based reuse of agent tool
traces: https://www.ycombinator.com/companies/memorable . Its public Cowork
plugin is MIT-licensed; the published core CLI carries a separate proprietary
license. Direct evaluation requires authorization appropriate to the intended
comparison. Its public figures are vendor claims, not Provena benchmark results.

Workflow-learning research informing the independent design includes Agent
Workflow Memory (https://arxiv.org/abs/2409.07429) and Memp
(https://arxiv.org/abs/2508.06433). Permissively licensed external benchmark
sources can be evaluated with their licenses and exact revisions recorded.

## Execution order

1. Isolated main-based branch; integrate existing security work and resolve
   conflicts preserving both contracts.
2. Dependency, packaging, and runtime updates with lockfile/advisory evidence.
3. Procedure/outcome kernel; retrieval eligibility and freshness regressions.
4. CLI/MCP interfaces, independent fixture evaluation, and use documentation.
5. Full relevant validation and reviewer gate; PR publication and main merge.
6. Fresh-install/main verification and comparative evidence audit. Any
   remaining unmeasured superiority requirement remains open.
