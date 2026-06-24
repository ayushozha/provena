import type { SearchResponse, SearchResult, SourceReference } from "./client.js";

const EXCERPT_MAX = 120;

/** True when ANSI styling is allowed (respects NO_COLOR; off on Windows by default). */
export function colorEnabled(): boolean {
  if (process.env.NO_COLOR !== undefined) {
    return false;
  }
  if (process.env.FORCE_COLOR !== undefined) {
    return true;
  }
  if (process.platform === "win32") {
    return false;
  }
  return process.stdout.isTTY === true;
}

function ansi(code: string, text: string, enabled: boolean): string {
  if (!enabled) {
    return text;
  }
  return `\u001b[${code}m${text}\u001b[0m`;
}

export function spanToLineDisplay(spanStart?: number, spanEnd?: number): string {
  if (spanStart === undefined || spanStart === null) {
    return "?";
  }
  if (spanEnd !== undefined && spanEnd !== spanStart) {
    return `${spanStart}-${spanEnd}`;
  }
  return String(spanStart);
}

export function primarySourceRef(result: SearchResult): SourceReference | undefined {
  const refs = result.memory.source_references;
  if (!refs?.length) {
    return undefined;
  }
  return refs[0];
}

/** Display path with line range from the first source reference. */
export function formatLocation(result: SearchResult): string {
  const ref = primarySourceRef(result);
  if (!ref) {
    return result.memory.title ?? result.memory.memory_id;
  }
  const path = ref.source_id || ref.title || ref.uri || "?";
  const lines = spanToLineDisplay(ref.span_start, ref.span_end);
  return `${path}:${lines}`;
}

export function truncateExcerpt(text: string, max = EXCERPT_MAX): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  if (oneLine.length <= max) {
    return oneLine;
  }
  return `${oneLine.slice(0, max - 1)}…`;
}

export function pickExcerpt(result: SearchResult, max = EXCERPT_MAX): string {
  const ref = primarySourceRef(result);
  const text = ref?.excerpt?.trim() || result.memory.content.trim();
  return truncateExcerpt(text, max);
}

export interface FormatSearchTableOptions {
  colors?: boolean;
}

/** Pretty terminal table for ranked search hits. */
export function formatSearchTable(
  results: SearchResult[],
  options: FormatSearchTableOptions = {},
): string {
  if (results.length === 0) {
    return "No results.";
  }

  const colors = options.colors ?? colorEnabled();
  const lines: string[] = [];

  for (let index = 0; index < results.length; index += 1) {
    const result = results[index];
    const rank = String(index + 1).padStart(2, " ");
    const score = result.score.toFixed(3).padStart(6, " ");
    const location = formatLocation(result);
    const excerpt = pickExcerpt(result);

    const rankLabel = ansi("2", rank, colors);
    const scoreLabel = ansi("33", score, colors);
    const locationLabel = ansi("36", location, colors);

    lines.push(`${rankLabel}  ${scoreLabel}  ${locationLabel}`);
    lines.push(`      ${excerpt}`);
    if (result.reasons.length > 0) {
      const reasons = result.reasons.join(", ");
      lines.push(`      ${ansi("2", reasons, colors)}`);
    }
    if (index < results.length - 1) {
      lines.push("");
    }
  }

  return lines.join("\n");
}

export function formatSearchJson(response: SearchResponse): string {
  return `${JSON.stringify(response, null, 2)}\n`;
}