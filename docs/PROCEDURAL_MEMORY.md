# Procedural memory

Provena records reusable repository procedures with ordered tool steps, source
citations, explicit approval, and outcomes tied to one exact procedure version.
It stores reference data: learning, approval, recall, and inspection never
execute recorded steps or grant permission to use a tool.

This implementation works offline in the repository ledger. It does not need
an LLM provider, the Python service, or a database server.

## Lifecycle and evidence

1. `learn` stores a candidate from an explicitly supplied structured trace.
   It reads the actual cited UTF-8 files and records their LF-canonical SHA-256
   text fingerprints, using the same normalization as the repository map.
2. A reviewer inspects the candidate. `approve` creates a new, human-authority
   version that supersedes the candidate. Approval does not record task success.
3. A caller performs and checks the actual task outside Provena, then records
   a `success`, `failure`, or `unknown` outcome against the approved version ID.
4. Recall returns a ready procedure only when it matches the task, its latest
   exact-version outcome reports success, its cited source text is current,
   and its declared prerequisites are confirmed.

Outcome evidence is **caller-reported attestation**. Provena validates the
receipt's structure and version link; it does not authenticate the caller's
claims, rerun goal checks, or independently prove task completion. A successful
command exit alone does not establish that the user's goal was met. Human
approval is an explicit local action, not an identity verification service.
Applications using the exported approval API must enforce their own reviewer
authorization.

Learning episode IDs and outcome receipt IDs are idempotency keys. Repeating
the same payload returns `duplicate: true`; changing its content or version
under the same key fails. An outcome episode cannot be recorded under another
receipt. A newly learned and approved procedure needs its own outcome evidence;
successes do not transfer from another version.

## CLI

Use an installed or built CLI in the repository containing the cited sources.
Initialize its local configuration first. For initialization without persistent
agent, hook, MCP, runtime, or daemon integrations:

```powershell
provena init --no-agents --no-hooks --no-mcp --no-runtime --no-daemon
```

Save a trace such as the following as `trace.json`. Adapt the goal, paths, tools,
and steps to the actual trace. The empty verification list makes no claim that
a task has succeeded.

```json
{
  "title": "Validate session handling",
  "episodeId": "learn-session-001",
  "sessionId": "coding-session-001",
  "goal": "Validate session handling before a handoff",
  "triggers": ["session validation"],
  "prerequisites": [{ "tool": "read" }, { "tool": "shell" }],
  "sources": [{ "path": "src/session.ts", "startLine": 1 }],
  "steps": [
    { "tool": "read", "args": { "path": "src/session.ts" } },
    { "tool": "shell", "args": { "command": "npm test" } }
  ],
  "verification": [],
  "sensitivity": "internal"
}
```

```powershell
provena procedure learn --file .\trace.json --actor reviewer
provena procedure inspect <candidate-id> --tool read --tool shell --json
provena procedure approve <candidate-id> --authority human --actor reviewer
```

Use the **approved event ID** returned by `approve` in subsequent receipts. The
candidate ID is not an eligible outcome target. After the caller independently
checks the actual goal, prepare `receipt.json` using the observed evidence.
The following is a template; replace its placeholders with real task evidence
before submitting a success receipt.

```json
{
  "procedureId": "<approved-event-id>",
  "receiptId": "goal-check-001",
  "episodeId": "task-episode-001",
  "sessionId": "coding-session-002",
  "goal": "Validate session handling before a handoff",
  "outcome": "success",
  "verification": [
    {
      "check": "<specific assertion of the task goal>",
      "passed": true,
      "evidence": "<actual observed goal-check output or receipt reference>"
    }
  ]
}
```

```powershell
provena procedure outcome --file .\receipt.json --actor task-runner
provena procedure recall "session validation" --tool read --tool shell --json
provena procedure recall "session validation" --include-review --max-tokens 1500
provena procedure inspect <approved-event-id> --tool read --tool shell --json
```

Success requires at least one verification entry and all entries must pass.
Failure requires a failed verification entry or a nonempty `failure` explanation.
Unknown outcomes can have an empty verification list. A latest failure blocks
ready recall; a later success for that same version can restore readiness while
keeping previous failure counts and citations. A latest unknown outcome leaves
the version unverified.

| Command | Behavior |
| --- | --- |
| `learn --file <json>` | Store a candidate, source hashes, ordered steps, and trace evidence. |
| `approve <candidate-id> --authority human` | Create an explicitly approved successor after review. |
| `outcome --file <json>` | Append a caller-reported exact-version outcome. |
| `recall <query>` | Return relevant ready pointers; `--include-review` also exposes blocked items. |
| `inspect <id>` | Select one active version directly and include its readiness, reasons, citations, and steps. |

`--actor` overrides `PROVENA_ACTOR`; the fallback is `local-user`. Recall and
inspection accept repeated `--tool` and `--path`, `--limit` (1–32),
`--max-tokens` (128–25,000), and `--json`. The default CLI token budget is 1,500
and the default item limit is eight. Inspection is subject to these budgets;
increase the budget if the selected procedure is omitted.

## Exported API

The CLI package exports `learnProcedure`, `approveProcedure`,
`recordProcedureOutcome`, and `recallProcedures`, their input/result types, and
the shared Zod validators `learnProcedureInputSchema` and
`procedureOutcomeInputSchema`.

```typescript
import {
  learnProcedure, approveProcedure, recordProcedureOutcome, recallProcedures,
} from "@provena/cli";

const candidate = await learnProcedure(root, { ...trace, actor: "trace-caller" });
// Only an authorized reviewer should invoke this API after inspecting the candidate.
const approved = await approveProcedure(root, {
  procedureId: candidate.event.id, actor: "reviewer",
});
await recordProcedureOutcome(root, {
  ...actualReceipt, procedureId: approved.event.id, actor: "task-caller",
});
const result = await recallProcedures(root, "session validation", {
  availableTools: ["read", "shell"], includeReview: true, maxTokens: 1500,
});
```

`root` identifies the repository whose ledger and cited source files are used.
Write operations return `{ event, duplicate }`. Recall returns `ready`, `review`,
`omitted`, `memoryFingerprint`, `characters`, and `estimatedTokens`. Each item
has an exact `procedureId`, a compact `pointer`, source and outcome `citations`,
reliability counts, reasons, and the complete structured `procedure`.

API recall also accepts exact `procedureIds`, `paths`, a caller-declared
`environment` record, and `maxCharacters`. Declared environment prerequisites
require an exact supplied value. CLI and MCP do not currently supply environment
values, so those prerequisites remain unconfirmed through those interfaces.
The API includes review items unless `includeReview: false` is supplied; CLI
recall and MCP default to ready items only. Source freshness always reads the
current files, including when stored map/graph artifacts are supplied. Readiness
always reads the current repository ledger. A supplied API `snapshot` is checked
for a valid attestation but cannot override current active versions or receipts;
cached success cannot bypass a later failure, retraction, or supersession.

## MCP

The repository MCP server exposes three procedure tools:

| Tool | Inputs and result |
| --- | --- |
| `provena_procedure_learn` | The learning payload without `actor`; returns the candidate event and duplicate status. |
| `provena_procedure_outcome` | The receipt payload without `actor`; returns the outcome event and duplicate status. |
| `provena_procedure_recall` | `query`, `paths`, `procedureIds`, `availableTools`, `includeReview`, `maxTokens`, `maxItems`; returns the recall result. |

MCP records its actor as `mcp-client`. It exposes no human approval tool.
A reviewer uses the CLI's explicit approval action. Use `procedureIds` with
`includeReview: true` to inspect a version through recall. Learning and outcome
tools refresh derived repository artifacts after writing; recall remains read
only and checks current source bytes without a refresh.

## Retrieval gates and limits

Query relevance uses lexical overlap with the title, goal, and triggers. A query
with multiple searchable tokens needs at least two matches; an exact active
procedure ID or applicable source path can select a procedure directly. Outcome
reliability and small importance/graph priors only rank relevant items. They do
not cause unrelated procedures to appear.

Blocked items appear in `review` when requested. Their states include
`candidate`, `unverified`, `failed`, `stale`, and `incompatible`. Changed,
missing, unreadable, or unsafe cited sources make an item stale. Declared file
prerequisites must exist; tool prerequisites need the caller's available tool
names. When the caller supplies a tool set, it must contain every recorded step's
tool. Tool names establish declared availability, not tool argument-schema
compatibility or permission to run a command.

The kernel bounds steps to 32, cited source files to 32, and each cited file to
1 MiB. It rejects malformed payloads, nonportable or unsafe source paths,
denied secret paths, known secret material, oversized JSON, and overly nested
arguments. These safeguards do not guarantee that all sensitive content can be
detected; callers must review captured data before writing or sharing it.

Response budgets use the existing approximate rule of four characters per
token and count the serialized JSON result. Entire procedure items are omitted
when they do not fit; their ordered steps are not truncated into incomplete
instructions. This estimate is not a provider-specific tokenizer measurement.

Procedures use the existing append-only ledger kinds: `workflow` events carry
`structured_data.procedure`; `fact` or `mistake` events carry
`structured_data.procedureOutcome`. Payload `schemaVersion` is 1. The approved
event ID identifies the version, and source `blob` values use `sha256-lf:<digest>`.
The digest is calculated after strict UTF-8 decoding and CRLF or CR newline
normalization to LF; newline-only checkout differences do not invalidate a
procedure. Other text changes still invalidate its cited evidence. The tag
distinguishes these canonical text fingerprints from raw-byte hashes. This new,
unpublished procedure layer does not claim migration support for earlier
experimental fingerprint formats.
Historical events remain in the ledger; recall selects current active procedure
versions and current active exact-version receipts. Historical snapshot readiness
is not supported.

Static workflow and learning views present procedural entries as inspection
references. They omit stored goals and steps and explicitly state that the view
has not checked current readiness. An outcome reference points to its exact
procedure version for inspection and retains the outcome's own ledger ID.

## Validation and measured scope

From `cli/`, build and run the focused checks:

```powershell
npm run build
node .\tests\procedures.test.mjs
node .\tests\procedure-cli.test.mjs
node .\tests\mcp-server.test.mjs
node .\eval\procedure-behavior.mjs
```

The current independent behavior fixture has **13 cases: 13 passed, zero failed,
zero skipped**. It covers relevance, abstention, approval, missing or unknown
receipts, failures and recovery, freshness, prerequisites, and whole-response
budgets. The fixture uses synthetic caller attestations and performs no agent
task execution. Dataset SHA-256:
`fc76eae217e5d788ed6e4005e7edb20d290ded355beed5498c480b41f14d564c`.
The report includes every case in its denominator.

These results establish the tested eligibility behavior. They do not measure
task success, token savings, or superiority to Memorable, Mem0, Letta, Zep, or
another memory system. **No external competitor has been measured by this
fixture.** Full task execution with independent goal checks and equal budgets
is required for such a comparison.

Opt-in [native tool capture](./TOOL_CAPTURE.md) records filtered local
observations from Codex and Claude hooks and prepares incomplete review drafts.
Learning still requires an explicitly supplied, reviewed structured trace.
Semantic workflow abstraction, graph composition of multiple procedures, and
an autonomous replay executor are not implemented. The current layer provides
cited, inspectable guidance for a caller to review and use.
