import { isAbsolute, resolve } from "node:path";
import { getGitRoot } from "../config.js";
import { realpathSync } from "node:fs";
import {
  CAPTURE_LIMITS, captureHook, draftCapturedEpisode, listCapturedEpisodes, parseCapturePayload,
} from "../capture/index.js";
import {
  captureRepoRoot, installCaptureHooks, uninstallCaptureHooks, type CaptureProvider,
} from "../integrations/capture-hooks.js";
import { assertSafeRepoPath } from "../security/paths.js";

const VALUES = new Set(["--provider", "--root", "--goal", "--source", "--title", "--call"]);
const SWITCHES = new Set(["--json", "--abstract", "--help", "-h"]);
function parseArgs(args: string[]): { positional: string[]; flags: Map<string, string[]> } {
  const positional: string[] = [];
  const flags = new Map<string, string[]>();
  for (let i = 0; i < args.length; i++) {
    const item = args[i]!;
    if (VALUES.has(item)) {
      const value = args[++i];
      if (!value || value.startsWith("--")) throw new Error("capture option requires a value");
      flags.set(item, [...(flags.get(item) ?? []), value]);
    } else if (SWITCHES.has(item)) {
      if (flags.has(item)) throw new Error("duplicate capture option");
      flags.set(item, []);
    }
    else if (item.startsWith("-")) throw new Error("unknown capture option");
    else positional.push(item);
  }
  for (const [key, values] of flags) if (values.length > 1 && key !== "--source" && key !== "--call") throw new Error("duplicate capture option");
  return { positional, flags };
}
export function printCaptureHelp(): void {
  console.log([
    "Usage: provena capture <install|uninstall|hook|list|draft> [options]",
    "  install|uninstall --provider codex|claude [--root <repository>]",
    "  hook --provider codex|claude --root <absolute-installed-root>  Read one native hook JSON object from stdin",
    "  list [--json]                              List local tool episodes without raw inputs",
    "  draft <episode-id> --goal <goal> --source <path> [--source <path>] [--call <id>] [--title <title>] [--abstract]",
    "  --abstract                                Explicitly send filtered selected observations to config.intelligence_url for review-only abstraction",
    "  --root <repository> --json                  Select repository and machine-readable output",
    "Capture is opt-in and local. Drafts require review; tool completion never proves task success.",
    "Review a draft and its candidate payload before using procedure learn. No command approves or executes stored steps.",
  ].join("\n"));
}

async function readStdin(): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  const timer = setTimeout(() => process.stdin.destroy(new Error("capture stdin deadline exceeded")), 10_000);
  timer.unref();
  try {
    for await (const chunk of process.stdin) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += bytes.length;
      if (size > CAPTURE_LIMITS.payloadBytes) {
        process.stdin.destroy();
        throw new Error("capture stdin exceeds its byte limit");
      }
      chunks.push(bytes);
    }
    return Buffer.concat(chunks, size);
  } finally { clearTimeout(timer); }
}

export async function runCaptureCommand(args: string[], cwd = process.cwd()): Promise<number> {
  // Native observers must never block tools, emit raw client data, or send agent-control decisions.
  const isHook = args[0] === "hook";
  try {
    const { positional, flags } = parseArgs(args);
    if (isHook && (flags.has("--help") || flags.has("-h"))) throw new Error("hook does not emit help text");
    if (!positional.length || flags.has("--help") || flags.has("-h")) {
      printCaptureHelp();
      return 0;
    }
    const [command, ...rest] = positional;
    const last = (key: string) => flags.get(key)?.at(-1);
    const rawRoot = last("--root");
    if (isHook && (!rawRoot || !isAbsolute(rawRoot))) throw new Error("hook requires its installed absolute root");
    const root = captureRepoRoot(rawRoot ? resolve(cwd, rawRoot) : resolve(getGitRoot(cwd)));
    if (isHook) {
      assertSafeRepoPath(root, resolve(cwd));
      if (realpathSync.native(getGitRoot(cwd)) !== realpathSync.native(root)) throw new Error("hook invoked from another checkout");
    }
    const optionSets: Record<string, Set<string>> = {
      install: new Set(["--provider", "--root", "--json"]), uninstall: new Set(["--provider", "--root", "--json"]),
      hook: new Set(["--provider", "--root"]), list: new Set(["--root", "--json"]),
      draft: new Set(["--root", "--json", "--goal", "--source", "--title", "--call", "--abstract"]),
    };
    const allowed = optionSets[command!];
    if (!allowed || [...flags.keys()].some((flag) => !allowed.has(flag))) throw new Error("unsupported capture command or option");
    if (command !== "draft" && rest.length || command === "draft" && rest.length !== 1) throw new Error("unexpected capture arguments");
    let result: unknown;
    if (command === "install" || command === "uninstall" || command === "hook") {
      const provider = last("--provider");
      if (provider !== "codex" && provider !== "claude") throw new Error("capture provider must be codex or claude");
      if (command === "hook") {
        await captureHook(root, provider, parseCapturePayload(await readStdin()));
        process.stdout.write("{}\n");
        return 0;
      }
      result = await (command === "install" ? installCaptureHooks : uninstallCaptureHooks)(root, provider as CaptureProvider);
    } else if (command === "list") result = await listCapturedEpisodes(root);
    else result = await draftCapturedEpisode(root, {
      episodeId: rest[0]!, goal: last("--goal") ?? "", sources: flags.get("--source") ?? [],
      title: last("--title"), observationIds: flags.get("--call"), abstract: flags.has("--abstract"),
    });
    console.log(JSON.stringify(result, null, flags.has("--json") ? undefined : 2));
    return 0;
  } catch (error) {
    if (!isHook) throw error;
    process.stderr.write("provena capture: observation not recorded; check local setup, payload limits and repository boundaries.\n");
    process.stdout.write("{}\n");
    return 0;
  }
}
