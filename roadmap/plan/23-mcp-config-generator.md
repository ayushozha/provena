# PLAN-23: MCP Config Generator

## Goal

`provena mcp install` writes Cursor/Claude MCP server config pointing at the
local polyglot MCP server (or standalone tool bridge) scoped to the current repo.

## Why this is its own plan

MCP setup is distribution/UX — JSON config generation, paths, API keys — separate
from memory indexing logic.

## Prerequisites

- PLAN-02 complete (config + scope)
- PLAN-09 complete (indexed repo recommended)
- MCP server buildable (`control-plane/cmd/mcp`)

## Success criteria

- [ ] `provena mcp install --cursor` writes or merges into `.cursor/mcp.json`
- [ ] `provena mcp install --claude` prints snippet for `claude_desktop_config.json`
- [ ] Config includes: MCP server command, `PROVENA_API_URL`, scope tenant/project
- [ ] `provena mcp doctor` verifies MCP server `/healthz` or handshake
- [ ] Document 9 existing tools: memory_create, memory_search, memory_get, etc.
- [ ] Idempotent install does not duplicate server entry

## Scope

### In scope

- `cli/src/commands/mcp.ts`
- Templates for Cursor and Claude Desktop config shapes
- Option `--standalone` pointing at Python store only (limited tools doc)

### Out of scope

- Publishing MCP server to npm
- Custom tool definitions for code graph (future)

## Implementation

### Steps

1. Detect editor config paths OS-aware (`%APPDATA%`, `~/.cursor/`).
2. Merge JSON array of mcpServers without clobbering unrelated servers.
3. For local dev: command = `go run ./control-plane/cmd/mcp` or docker compose mcp.
4. Inject API key from env or prompt to hash into gateway config doc link.
5. Print "restart Cursor" instruction.

## Files to create or modify

| Path | Action |
|------|--------|
| `cli/src/commands/mcp.ts` | create |
| `cli/templates/mcp-cursor.json` | create |
| `cli/templates/mcp-claude.json` | create |
| `DEPLOYMENT.md` | MCP quickstart cross-link |

## Verification

```powershell
provena mcp install --cursor --dry-run
provena mcp doctor
# manual: restart Cursor, verify tools list in agent
```

## Handoff to next plan

Developers get: init → index → mcp install → agent queries repo memory.