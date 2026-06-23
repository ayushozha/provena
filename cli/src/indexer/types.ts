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