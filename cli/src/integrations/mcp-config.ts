import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { assertSafeRepoPath } from "../security/paths.js";
import { canonicalJson } from "../brain/utils.js";

const CODEX_START = "# >>> provena MCP >>>";
const CODEX_END = "# <<< provena MCP <<<";
const SERVER = {
  command: "node",
  args: [
    ".provena/runtime/node_modules/@provena/cli/dist/cli.js",
    "mcp",
    "serve",
  ],
} as const;

export interface McpConfigResult {
  client: "claude" | "cursor" | "codex" | "vscode";
  path: string;
  action: "created" | "updated" | "unchanged" | "skipped";
  reason?: string;
}

function writeAtomic(repoRoot: string, path: string, content: string): void {
  assertSafeRepoPath(repoRoot, dirname(path));
  assertSafeRepoPath(repoRoot, path);
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.provena-${process.pid}.tmp`;
  assertSafeRepoPath(repoRoot, temporary);
  try {
    writeFileSync(temporary, content, { encoding: "utf8", mode: 0o600 });
    assertSafeRepoPath(repoRoot, temporary);
    assertSafeRepoPath(repoRoot, path);
    renameSync(temporary, path);
  } finally {
    assertSafeRepoPath(repoRoot, temporary);
    rmSync(temporary, { force: true });
  }
}

function mergeJsonServer(
  repoRoot: string,
  path: string,
  key: "mcpServers" | "servers",
): McpConfigResult["action"] {
  assertSafeRepoPath(repoRoot, path);
  const existing = existsSync(path) ? readFileSync(path, "utf8") : "";
  let parsed: Record<string, unknown> = {};
  if (existing.trim()) {
    try {
      const value = JSON.parse(existing) as unknown;
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("config root must be a JSON object");
      }
      parsed = value as Record<string, unknown>;
    } catch {
      throw new Error(`cannot merge Provena into invalid JSON config: ${path}`);
    }
  }
  if (
    Object.prototype.hasOwnProperty.call(parsed, key) &&
    (!parsed[key] || typeof parsed[key] !== "object" || Array.isArray(parsed[key]))
  ) {
    throw new Error(`existing ${key} in ${path} must be a JSON object`);
  }
  const current = (parsed[key] as Record<string, unknown> | undefined) ?? {};
  if (
    Object.prototype.hasOwnProperty.call(current, "provena") &&
    canonicalJson(current.provena) !== canonicalJson(SERVER)
  ) {
    throw new Error(`existing user-owned Provena MCP entry in ${path}`);
  }
  const next = `${JSON.stringify(
    { ...parsed, [key]: { ...current, provena: SERVER } },
    null,
    2,
  )}\n`;
  if (next === existing) return "unchanged";
  writeAtomic(repoRoot, path, next);
  return existing ? "updated" : "created";
}

function mergeCodex(repoRoot: string, path: string): McpConfigResult["action"] {
  assertSafeRepoPath(repoRoot, path);
  const existing = existsSync(path) ? readFileSync(path, "utf8") : "";
  const block = [
    CODEX_START,
    "[mcp_servers.provena]",
    'command = "node"',
    'args = [".provena/runtime/node_modules/@provena/cli/dist/cli.js", "mcp", "serve"]',
    CODEX_END,
  ].join("\n");
  const start = existing.indexOf(CODEX_START);
  const end = existing.indexOf(CODEX_END);
  let next: string;
  if (start >= 0 || end >= 0) {
    if (start < 0 || end < start) {
      throw new Error(`malformed Provena managed block in ${path}`);
    }
    next = `${existing.slice(0, start)}${block}${existing.slice(end + CODEX_END.length)}`;
  } else {
    if (/^\s*\[mcp_servers\.provena\]\s*$/m.test(existing)) {
      throw new Error(`existing user-owned [mcp_servers.provena] table in ${path}`);
    }
    const separator = !existing ? "" : existing.endsWith("\n") ? "\n" : "\n\n";
    next = `${existing}${separator}${block}\n`;
  }
  if (next === existing) return "unchanged";
  writeAtomic(repoRoot, path, next);
  return existing ? "updated" : "created";
}

export function installMcpConfigs(repoRoot: string): McpConfigResult[] {
  const targets: McpConfigResult[] = [];
  const attempt = (
    client: McpConfigResult["client"],
    path: string,
    install: () => McpConfigResult["action"],
  ) => {
    try {
      targets.push({ client, path, action: install() });
    } catch (error) {
      targets.push({
        client,
        path,
        action: "skipped",
        reason: error instanceof Error ? error.message : String(error),
      });
    }
  };
  const claude = join(repoRoot, ".mcp.json");
  attempt("claude", ".mcp.json", () => mergeJsonServer(repoRoot, claude, "mcpServers"));
  const cursor = join(repoRoot, ".cursor", "mcp.json");
  attempt("cursor", ".cursor/mcp.json", () => mergeJsonServer(repoRoot, cursor, "mcpServers"));
  const vscode = join(repoRoot, ".vscode", "mcp.json");
  attempt("vscode", ".vscode/mcp.json", () => mergeJsonServer(repoRoot, vscode, "servers"));
  const codex = join(repoRoot, ".codex", "config.toml");
  attempt("codex", ".codex/config.toml", () => mergeCodex(repoRoot, codex));
  return targets;
}

export function installedMcpClients(repoRoot: string): McpConfigResult["client"][] {
  const clients: McpConfigResult["client"][] = [];
  const jsonTargets: Array<[
    McpConfigResult["client"],
    string,
    "mcpServers" | "servers",
  ]> = [
    ["claude", join(repoRoot, ".mcp.json"), "mcpServers"],
    ["cursor", join(repoRoot, ".cursor", "mcp.json"), "mcpServers"],
    ["vscode", join(repoRoot, ".vscode", "mcp.json"), "servers"],
  ];
  for (const [client, path, key] of jsonTargets) {
    try {
      assertSafeRepoPath(repoRoot, path);
      const parsed = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
      const servers = parsed?.[key];
      if (
        servers &&
        typeof servers === "object" &&
        !Array.isArray(servers) &&
        canonicalJson((servers as Record<string, unknown>).provena) === canonicalJson(SERVER)
      ) clients.push(client);
    } catch {
      // Missing, invalid, or unsafe user-owned configs are not installed.
    }
  }
  const codex = join(repoRoot, ".codex", "config.toml");
  try {
    assertSafeRepoPath(repoRoot, codex);
    const text = readFileSync(codex, "utf8");
    if (text.includes(CODEX_START) && text.includes(CODEX_END)) clients.push("codex");
  } catch {
    // Not installed.
  }
  return clients;
}
