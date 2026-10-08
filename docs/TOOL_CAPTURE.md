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

At a quota limit, later observations are rejected and an existing episode is
marked incomplete with `quotaReached`. Hook stdout is `{}`; diagnostics are
generic and never echo the raw payload. The observer returns no tool-control
or permission decision. Best-effort credential checks and filtering cannot
detect every sensitive string; review before learning, committing, or sharing.

Completion order is not causal order. Native coverage can be partial, and
parallel calls may complete out of sequence. Failed attempts and later recovery
remain visible; every draft requires review. Semantic abstraction, automatic
learning/approval, and replay execution are not part of this adapter.

## Validation and client references

From `cli/`, run `npm run build` and `node tests/capture.test.mjs` for the
focused adapter, boundary, config-preservation, and draft checks. Fixtures
exercise documented payload shapes; they do not by themselves prove native
client delivery or an agent's task success.
Run `node tests/capture-draft-pair.test.mjs` for draft privacy, handled write
failure, and ignore-rule change regressions.

The native schemas are documented in [Codex hooks](https://learn.chatgpt.com/docs/hooks)
and [Claude Code hooks](https://code.claude.com/docs/en/hooks). Hook availability,
trust, policy and tool-response shapes depend on the installed client version.
