# Opt-in coding-tool capture

Provena can observe Codex and Claude Code tool-completion hooks in one Git
checkout. It retains filtered local observations and prepares procedure drafts
for review. It does not infer the user's goal, execute captured tools, approve
a procedure, or establish that a task succeeded.

## Enable and remove

Install Provena in the target repository and initialize its persisted runtime.
Tool capture is not enabled by `init`:

After upgrading the installed CLI, rerun `init` before installing capture.
Runtime reuse checks the invoking package's executable and dependency contents
as well as the persisted tree, including updates that keep the same prerelease
package version. Older integrity metadata triggers a fresh offline runtime copy.

```powershell
npx provena init --no-daemon
npx provena capture install --provider codex --json
# Or, for Claude Code:
npx provena capture install --provider claude --json
```

The commands merge only the managed entries into `.codex/hooks.json` or
`.claude/settings.local.json`. Other entries and trust settings are preserved.
Codex uses `PostToolUse`; Claude uses `PostToolUse` and `PostToolUseFailure`.
Review the exact hook definition in the native client's `/hooks` interface.
Installation does not bypass trust, enable disabled hooks, or override managed
policy. The native client must deliver the event for an observation to exist.

The installed hook invokes the ignored persisted runtime with a fixed absolute
checkout root. The root is independent of the hook's reported `cwd`. Both must
identify the same Git working tree. Config files, source references, and local
cache cannot traverse out of that checkout or follow managed symlinks.

```powershell
npx provena capture uninstall --provider codex --json
npx provena capture uninstall --provider claude --json
```

Uninstall removes only exact entries owned by this installation. It retains
local episodes for inspection. An edited managed entry is preserved rather
than claimed as Provena's own. Config conflicts, unsupported path quoting,
missing runtime, or unsafe cache settings return `action: "skipped"` with a
setup explanation. No global agent configuration is edited.

## Inspect and draft

After the client has delivered tool activity:

```powershell
npx provena capture list --json
$Episode = "episode-id-returned-by-list"
npx provena capture draft $Episode `
  --goal "Validate session handling before handoff" `
  --source src/session.ts `
  --json
```

Drafting requires an explicit goal and one to 32 current source files. Select
one to 32 observations with repeated `--call <observation-id>` when the session
contains more calls or unrelated work. The returned `draftPath` contains the
observation statuses, warnings, source evidence, and candidate payload;
`candidatePath` contains the payload accepted by `procedure learn`.

Review the wrapper and candidate locally. Complete or remove redacted steps,
check the ordering, and verify applicability against current source. Only then
learn the candidate using the returned path:

```powershell
$Candidate = "candidatePath-returned-by-draft"
npx provena procedure learn --file $Candidate --actor reviewer
```

This explicit learning action moves reviewed data into the tracked ledger.
Human approval and an independent exact-version task receipt remain separate
steps in the [procedure lifecycle](./PROCEDURAL_MEMORY.md). Drafts have
`verification: []`, `complete: false`, and `taskOutcome: "unknown"`. A successful
tool exit never becomes a successful task receipt.

## Optional grounded abstraction

Ordinary drafts and installed hooks remain offline. Add `--abstract` to an
explicit draft command to request help selecting relevant recorded steps:

```powershell
npx provena capture draft $Episode `
  --goal "Validate session handling before handoff" `
  --source src/session.ts `
  --abstract `
  --json
```

This requires `intelligence_url` in the existing `.provena/config.json` and a
configured intelligence service with model routing. In production, set this
URL to the authenticated gateway origin. The CLI uses the existing
`PROVENA_API_KEY` runtime environment variable for its client credential and
sends the configured tenant scope. The gateway authenticates that credential,
enforces write authority, and supplies the authoritative service identity.
Service and model-provider credentials stay on the server. A direct service
origin is supported only by the existing explicit local development bypass.
The URL must be an HTTP(S) origin without embedded credentials,
query parameters, or a path. A remote origin requires HTTPS and the existing
`PROVENA_ALLOW_REMOTE_STORE=1` opt-in. Redirects are rejected.

The request sends the explicit goal and one to 32 selected observations:
hashed call IDs, tool names, repository-relative working directories, filtered
safe arguments, tool statuses, and issue labels. Source contents, fingerprints,
raw outputs, transcripts, native identifiers, and environment values are not
sent. The configured service can pass this minimized data to its configured
model provider. Goals, command names and paths can still be sensitive; review
them before choosing this explicit egress option.

The service may suggest a title, triggers, retained observation IDs, omission
reasons, and a recovery summary. It cannot supply steps, arguments, source
paths, prerequisites, approval, or outcome receipts. The CLI verifies strict
schemas, an exact partition of the supplied IDs, and canonical request/response
hashes. It constructs candidate steps from the original retained observations
in recorded order. An explicit `--title` takes precedence over the suggested
title, and the caller's goal remains unchanged.

All original selected observations, including failed and omitted attempts,
remain in the local review wrapper alongside source evidence. Model recovery
text and model metadata stay in that wrapper as untrusted review notes; they
do not replace the candidate's deterministic failure warnings. Reported model
configuration and matching hashes are not authenticated proof of execution,
model quality, or task success.

The CLI releases its repository lock during the request, then rechecks the
entire episode and current source fingerprints under the lock before writing.
Changed or removed observations, changed sources, or unsafe cache paths cause
an error. Missing configuration, service failures, redirects, malformed or
oversized responses, compressed responses, and the request deadline also fail
explicitly. They create no draft and no ledger event; no offline result is
silently substituted. Successful abstraction still produces an incomplete,
unapproved draft with `taskOutcome: "unknown"` and `verification: []`.

Both draft destinations and their actual temporary paths are checked before
either payload is staged. If the second publish fails, the writer attempts to
restore previous destination bytes and remove its own temporary files. The two-file
write is not crash-atomic; interrupted processes, rollback I/O failures, or
concurrent filesystem ownership changes can require local draft review.
Caught write or rollback failures are reported explicitly. Draft writing never
creates a ledger event.

## Retained data and limits

Observations retain a hashed session/call identity, named tool, completion
status, bounded safe argument subset, and source fingerprint metadata. Known
test/build invocations, safe file paths and line ranges can be retained.
Arbitrary shell code, file contents, patch contents, prompts, descriptions,
environment values, raw tool output, permission metadata, and arbitrary MCP
arguments are omitted. Redacted or missing inputs and ambiguous statuses are
marked. Provena's own capture and memory calls are excluded to avoid recursion.

No transcript file or global private session store is read. Referenced local
UTF-8 source files are read only to compute LF-normalized SHA-256 fingerprints;
their contents are not stored in observations. Fingerprints distinguish
post-tool capture from draft time, and the learner fingerprints the current
source again. A draft-time fingerprint cannot prove what a previous tool saw.

The cache lives under ignored, untracked `.provena/cache/episodes/`. Capture
refuses to record if the cache is tracked or not ignored. Local installation
ownership is ignored too. Repository locks and atomic writes serialize
concurrent deliveries; replaying an identical tool-call identity is a no-op,
while conflicting filtered input or status fails closed.

Drafting checks both destination files and their temporary paths before
writing either payload. A handled second-file write failure restores the
previous draft bytes or removes the newly written first file. Owned temporary
files are cleaned up even if ignore rules change during the write. The pair
is not crash-atomic: process termination or a failed rollback can leave local
draft files that require review before learning or sharing.

| Limit | Value |
| --- | --- |
| Native JSON input | 256 KiB, depth eight, 128 entries per container |
| One episode | 128 observations, 256 KiB |
| Episodes in one checkout | 32, at most 2 MiB total |
| Local review drafts | 32 pairs, at most 80,000 bytes per file |
| One fingerprinted source | 1 MiB UTF-8 text |
| Stdin wait | Ten seconds |
| Explicit abstraction request / response | 80,000 bytes each in the CLI |
| Explicit abstraction deadline | Thirty seconds in the CLI |

At a quota limit, later observations are rejected and an existing episode is
marked incomplete with `quotaReached`. Hook stdout is `{}`; diagnostics are
generic and never echo the raw payload. The observer returns no tool-control
or permission decision. Best-effort credential checks and filtering cannot
detect every sensitive string; review before learning, committing, or sharing.

Completion order is not causal order. Native coverage can be partial, and
parallel calls may complete out of sequence. Failed attempts and later recovery
remain visible; every draft requires review. Optional abstraction is review
assistance. Automatic learning, approval, and replay execution are not provided.

## Validation and client references

From `cli/`, run `npm run build` and `node tests/capture.test.mjs` for the
focused adapter, boundary, config-preservation, and draft checks. Fixtures
exercise documented payload shapes; they do not by themselves prove native
client delivery or an agent's task success.
Run `node tests/capture-draft-pair.test.mjs` for draft privacy, handled write
failure, and ignore-rule change regressions.

`node tests/capture-abstraction.test.mjs` checks the explicit CLI path, minimized
egress, mocked authentication/transport limits, invalid proposals, source and
episode concurrency, and recorded cross-language hash fixtures. Mocked reports
do not measure actual provider quality or comparative task performance; those
measurements remain pending.

The native schemas are documented in [Codex hooks](https://learn.chatgpt.com/docs/hooks)
and [Claude Code hooks](https://code.claude.com/docs/en/hooks). Hook availability,
trust, policy and tool-response shapes depend on the installed client version.
