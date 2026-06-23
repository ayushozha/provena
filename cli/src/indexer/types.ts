/** Semantic chunk kinds produced by language-specific indexers. */
export type ChunkKind =
  | "module"
  | "import"
  | "class"
  | "function"
  | "method"
  | "interface"
  | "type_alias"
  | "artifact";

export interface CodeChunk {
  filePath: string;
  language: string;
  kind: ChunkKind;
  name: string | null;
  startLine: number;
  endLine: number;
  content: string;
  docstring?: string;
  imports?: string[];
  exported?: boolean;
  parentSymbol?: string;
}

/** Per-file metadata from discovery (PLAN-04). */
export interface FileMeta {
  /** Repo-relative path (POSIX-style). */
  path: string;
  absolutePath: string;
  sha256?: string;
}