import { z } from "zod";
import { canonicalJson, sha256 } from "../brain/utils.js";
import { assertNoSecretMaterial } from "../security/memory.js";

export const CAPTURE_ABSTRACTION_LIMITS = Object.freeze({ requestBytes: 80_000, responseBytes: 80_000 });
const id = z.string().regex(/^[a-f0-9]{64}$/);
// Supported by the minimum Node runtime; the project retains its older TS lib target.
const wellFormed = (value: string): boolean => (value as string & { isWellFormed(): boolean }).isWellFormed();
// Current Zod counts code points; the shared wire contract bounds UTF-16 units.
const text = (maximum: number) => z.string().min(1).max(maximum).refine((value) => value.trim().length > 0 && value.length <= maximum && wellFormed(value));
const argumentSchema = z.object({
  command: text(2_048).optional(), workdir: text(2_048).optional(),
  file_path: text(2_048).optional(), path: text(2_048).optional(),
  offset: z.number().int().min(0).max(1_000_000).optional(), limit: z.number().int().min(0).max(1_000_000).optional(),
  start_line: z.number().int().min(0).max(1_000_000).optional(), end_line: z.number().int().min(0).max(1_000_000).optional(),
}).strict();
export const captureAbstractionInputSchema = z.object({
  schemaVersion: z.literal(1), goal: z.string().trim().min(1).max(2_048).refine((value) => value.length <= 2_048 && wellFormed(value)),
  observations: z.array(z.object({
    id, tool: z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/), workingDirectory: text(2_048),
    args: argumentSchema, status: z.enum(["success", "failure", "unknown"]), issues: z.array(text(256)).max(8),
  }).strict()).min(1).max(32),
}).strict().refine((value) => new Set(value.observations.map((item) => item.id)).size === value.observations.length);
export const captureAbstractionResponseSchema = z.object({
  schemaVersion: z.literal(1), title: text(512), triggers: z.array(text(256)).max(8),
  keptObservationIds: z.array(id).min(1).max(32),
  omitted: z.array(z.object({ observationId: id, reason: text(512) }).strict()).max(32),
  recoverySummary: z.string().max(2_048).refine((value) => value.length <= 2_048 && wellFormed(value)), promptRevision: z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/),
  inputSha256: id, outputSha256: id,
  model: z.object({ provider: text(128), model: text(128), tier: z.enum(["fast", "balanced", "quality"]) }).strict(),
}).strict();
export type CaptureAbstractionInput = z.infer<typeof captureAbstractionInputSchema>;
export type CaptureAbstractionResponse = z.infer<typeof captureAbstractionResponseSchema>;

/** Wire hashes omit the ledger helper's trailing LF. This schema has ASCII keys and bounded integers. */
export const captureAbstractionSha256 = (value: unknown): string => sha256(canonicalJson(value).slice(0, -1));

/** Inspect parsed values: JSON escaping must not conceal an exact runtime credential. */
export function assertCaptureAbstractionCredentialFree(value: unknown, credential?: string): void {
  if (!credential) return;
  if (typeof value === "string") {
    if (value.includes(credential)) throw new Error("capture abstraction data contains the runtime credential");
    return;
  }
  if (value && typeof value === "object") {
    for (const item of Object.values(value)) assertCaptureAbstractionCredentialFree(item, credential);
  }
}

export function validateCaptureAbstraction(input: CaptureAbstractionInput, value: unknown): CaptureAbstractionResponse {
  const parsed = captureAbstractionResponseSchema.safeParse(value);
  if (!parsed.success) throw new Error("invalid capture abstraction response schema");
  const result = parsed.data;
  assertNoSecretMaterial(canonicalJson(result));
  const supplied = new Set(input.observations.map((item) => item.id));
  const retained = new Set(result.keptObservationIds);
  const omitted = new Set(result.omitted.map((item) => item.observationId));
  if (retained.size !== result.keptObservationIds.length || omitted.size !== result.omitted.length ||
      [...retained, ...omitted].some((item) => !supplied.has(item)) || [...retained].some((item) => omitted.has(item)) ||
      retained.size + omitted.size !== supplied.size) throw new Error("capture abstraction does not partition the supplied observations");
  const { outputSha256, ...output } = result;
  if (result.inputSha256 !== captureAbstractionSha256(input) || outputSha256 !== captureAbstractionSha256(output)) {
    throw new Error("capture abstraction provenance hash does not match its input or output");
  }
  return result;
}
