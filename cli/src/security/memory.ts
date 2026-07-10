const SECRET_PATTERNS = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/i,
  /\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{16,}\b/,
  /\bsk_[A-Za-z0-9_-]{16,}\b/,
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/,
  /\bgithub_pat_[A-Za-z0-9_]{20,}\b/,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/,
  /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/,
  /\bglpat-[A-Za-z0-9_-]{16,}\b/,
  /\bsk_live_[A-Za-z0-9]{16,}\b/,
  /\bAIza[A-Za-z0-9_-]{30,}\b/,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/,
  /\b[a-z][a-z0-9+.-]*:\/\/[^\s:/@]+:[^\s/@]+@/i,
  /\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s]{8,}/i,
];

/** Best-effort guard against common raw credentials; not a DLP boundary. */
export function assertNoSecretMaterial(value: string): void {
  if (SECRET_PATTERNS.some((pattern) => pattern.test(value))) {
    throw new Error(
      "memory appears to contain a credential or private key; store only the environment-variable name or secret-manager reference",
    );
  }
}
