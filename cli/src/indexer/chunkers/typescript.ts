import { createRequire } from "node:module";
import { basename, dirname, extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Language, Parser, type Node, type Tree } from "web-tree-sitter";
import type { ChunkKind, CodeChunk } from "../types.js";

const require = createRequire(fileURLToPath(import.meta.url));

type TsLanguage = "typescript" | "javascript" | "tsx" | "jsx";

const EXT_TO_LANGUAGE: Record<string, TsLanguage> = {
  ".ts": "typescript",
  ".mts": "typescript",
  ".cts": "typescript",
  ".tsx": "tsx",
  ".js": "javascript",
  ".mjs": "javascript",
  ".cjs": "javascript",
  ".jsx": "jsx",
};

const WASM_BY_LANGUAGE: Record<TsLanguage, { pkg: string; file: string }> = {
  typescript: {
    pkg: "tree-sitter-typescript",
    file: "tree-sitter-typescript.wasm",
  },
  tsx: {
    pkg: "tree-sitter-typescript",
    file: "tree-sitter-tsx.wasm",
  },
  javascript: {
    pkg: "tree-sitter-javascript",
    file: "tree-sitter-javascript.wasm",
  },
  jsx: {
    pkg: "tree-sitter-javascript",
    file: "tree-sitter-javascript.wasm",
  },
};

let parserInit: Promise<void> | null = null;
const languageCache = new Map<TsLanguage, Language>();
const parserCache = new Map<TsLanguage, Parser>();

function wasmPath(pkg: string, file: string): string {
  if (pkg === "web-tree-sitter") {
    const wasmUrl = import.meta.resolve("web-tree-sitter/web-tree-sitter.wasm");
    return join(dirname(fileURLToPath(wasmUrl)), file);
  }
  const pkgRoot = dirname(require.resolve(`${pkg}/package.json`));
  return join(pkgRoot, file);
}

async function ensureParserReady(): Promise<void> {
  if (!parserInit) {
    parserInit = Parser.init({
      locateFile(scriptName: string) {
        return wasmPath("web-tree-sitter", scriptName);
      },
    });
  }
  await parserInit;
}

async function loadLanguage(language: TsLanguage): Promise<Language> {
  const cached = languageCache.get(language);
  if (cached) {
    return cached;
  }
  const spec = WASM_BY_LANGUAGE[language];
  const loaded = await Language.load(wasmPath(spec.pkg, spec.file));
  languageCache.set(language, loaded);
  return loaded;
}

async function getParser(language: TsLanguage): Promise<Parser> {
  const cached = parserCache.get(language);
  if (cached) {
    return cached;
  }
  await ensureParserReady();
  const parser = new Parser();
  parser.setLanguage(await loadLanguage(language));
  parserCache.set(language, parser);
  return parser;
}

function detectLanguage(filePath: string): TsLanguage {
  const ext = extname(filePath).toLowerCase();
  return EXT_TO_LANGUAGE[ext] ?? "typescript";
}

function lineNumberAtIndex(content: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index && i < content.length; i += 1) {
    if (content.charCodeAt(i) === 10) {
      line += 1;
    }
  }
  return line;
}

function sliceByNode(content: string, node: Node): string {
  return content.slice(node.startIndex, node.endIndex);
}

function chunkFromNode(
  filePath: string,
  language: string,
  kind: ChunkKind,
  name: string | null,
  node: Node,
  content: string,
  extras: Partial<CodeChunk> = {},
): CodeChunk {
  return {
    filePath,
    language,
    kind,
    name,
    startLine: node.startPosition.row + 1,
    endLine: node.endPosition.row + 1,
    content: sliceByNode(content, node),
    ...extras,
  };
}

function isExported(node: Node): boolean {
  let current: Node | null = node;
  while (current) {
    if (current.type === "export_statement") {
      return true;
    }
    current = current.parent;
  }
  return false;
}

function leadingDocstring(content: string, node: Node): string | undefined {
  const lines = content.split(/\r?\n/);
  const startLine = node.startPosition.row;
  let line = startLine - 1;
  const collected: string[] = [];

  while (line >= 0) {
    const trimmed = lines[line]?.trim() ?? "";
    if (!trimmed) {
      if (collected.length > 0) {
        break;
      }
      line -= 1;
      continue;
    }
    if (
      trimmed.startsWith("/**") ||
      trimmed.startsWith("*") ||
      trimmed.startsWith("//") ||
      trimmed.endsWith("*/")
    ) {
      collected.unshift(lines[line] ?? "");
      line -= 1;
      continue;
    }
    break;
  }

  if (collected.length === 0) {
    return undefined;
  }
  return collected.join("\n").trim();
}

function declarationName(node: Node): string | null {
  const nameNode =
    node.childForFieldName("name") ??
    node.namedChildren.find(
      (child) => child.type === "identifier" || child.type === "type_identifier",
    );
  if (!nameNode) {
    return null;
  }
  return nameNode.text;
}

function collectImportSpecifiers(node: Node): string[] {
  const specifiers = new Set<string>();
  const queue: Node[] = [node];
  while (queue.length > 0) {
    const current = queue.shift();
    if (!current) {
      continue;
    }
    if (
      current.type === "import_clause" ||
      current.type === "identifier" ||
      current.type === "type_identifier"
    ) {
      const text = current.text.trim();
      if (text) {
        specifiers.add(text);
      }
    }
    for (const child of current.namedChildren) {
      queue.push(child);
    }
  }
  return [...specifiers].sort((a, b) => a.localeCompare(b));
}

function extractModuleSpecifier(importNode: Node): string | null {
  const source = importNode.childForFieldName("source");
  if (!source) {
    return null;
  }
  return source.text.replace(/^['"]|['"]$/g, "");
}

function collectImportChunks(
  filePath: string,
  language: string,
  root: Node,
  content: string,
): { importChunks: CodeChunk[]; importSpecifiers: string[] } {
  const importNodes = root.namedChildren.filter(
    (child) => child.type === "import_statement",
  );
  if (importNodes.length === 0) {
    return { importChunks: [], importSpecifiers: [] };
  }

  const first = importNodes[0]!;
  const last = importNodes[importNodes.length - 1]!;
  const importSpecifiers = importNodes.flatMap((node) => {
    const moduleName = extractModuleSpecifier(node);
    const localNames = collectImportSpecifiers(node);
    if (moduleName) {
      return localNames.length > 0
        ? localNames.map((name) => `${name} from "${moduleName}"`)
        : [`"${moduleName}"`];
    }
    return localNames;
  });

  const importChunk: CodeChunk = {
    filePath,
    language,
    kind: "import",
    name: null,
    startLine: first.startPosition.row + 1,
    endLine: last.endPosition.row + 1,
    content: content.slice(first.startIndex, last.endIndex),
    imports: [...new Set(importSpecifiers)].sort((a, b) => a.localeCompare(b)),
  };

  return {
    importChunks: [importChunk],
    importSpecifiers: importChunk.imports ?? [],
  };
}

function walkDeclarations(
  filePath: string,
  language: string,
  root: Node,
  content: string,
  chunks: CodeChunk[],
): void {
  for (const child of root.namedChildren) {
    if (child.type === "export_statement") {
      walkDeclarations(filePath, language, child, content, chunks);
      continue;
    }

    switch (child.type) {
      case "function_declaration":
      case "generator_function_declaration": {
        const name = declarationName(child);
        chunks.push(
          chunkFromNode(filePath, language, "function", name, child, content, {
            exported: isExported(child),
            docstring: leadingDocstring(content, child),
          }),
        );
        break;
      }
      case "class_declaration": {
        const name = declarationName(child);
        chunks.push(
          chunkFromNode(filePath, language, "class", name, child, content, {
            exported: isExported(child),
            docstring: leadingDocstring(content, child),
          }),
        );
        const classBody = child.childForFieldName("body");
        if (classBody && name) {
          for (const member of classBody.namedChildren) {
            if (
              member.type === "method_definition" ||
              member.type === "method_signature" ||
              member.type === "abstract_method_signature"
            ) {
              const methodName = declarationName(member);
              chunks.push(
                chunkFromNode(
                  filePath,
                  language,
                  "method",
                  methodName,
                  member,
                  content,
                  {
                    exported: isExported(child),
                    parentSymbol: name,
                    docstring: leadingDocstring(content, member),
                  },
                ),
              );
            }
          }
        }
        break;
      }
      case "interface_declaration": {
        const name = declarationName(child);
        chunks.push(
          chunkFromNode(filePath, language, "interface", name, child, content, {
            exported: isExported(child),
            docstring: leadingDocstring(content, child),
          }),
        );
        break;
      }
      case "type_alias_declaration": {
        const name = declarationName(child);
        chunks.push(
          chunkFromNode(filePath, language, "type_alias", name, child, content, {
            exported: isExported(child),
            docstring: leadingDocstring(content, child),
          }),
        );
        break;
      }
      case "lexical_declaration":
      case "variable_declaration": {
        const declarators = child.namedChildren.filter(
          (node) => node.type === "variable_declarator",
        );
        for (const declarator of declarators) {
          const init = declarator.childForFieldName("value");
          if (
            init &&
            (init.type === "arrow_function" ||
              init.type === "function_expression" ||
              init.type === "generator_function")
          ) {
            const nameNode = declarator.childForFieldName("name");
            const name = nameNode?.text ?? null;
            chunks.push(
              chunkFromNode(filePath, language, "function", name, declarator, content, {
                exported: isExported(child),
                docstring: leadingDocstring(content, child),
              }),
            );
          }
        }
        break;
      }
      default:
        break;
    }
  }
}

function buildModuleChunk(
  filePath: string,
  language: string,
  content: string,
  importSpecifiers: string[],
  tree: Tree,
): CodeChunk {
  const root = tree.rootNode;
  const moduleName = basename(filePath).replace(/\.[^.]+$/, "");
  const firstDeclaration = root.namedChildren.find(
    (child) => child.type !== "import_statement",
  );
  const headerEndIndex = firstDeclaration?.startIndex ?? content.length;
  const headerContent = content.slice(0, headerEndIndex).trimEnd();
  const headerDocTarget = firstDeclaration ?? root.namedChildren[0] ?? root;

  return {
    filePath,
    language,
    kind: "module",
    name: moduleName,
    startLine: 1,
    endLine: Math.max(1, lineNumberAtIndex(content, headerEndIndex)),
    content: headerContent.length > 0 ? headerContent : content.split(/\r?\n/)[0] ?? "",
    imports: importSpecifiers,
    docstring: leadingDocstring(content, headerDocTarget),
  };
}

function hasParseErrors(tree: Tree): boolean {
  const stack: Node[] = [tree.rootNode];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node) {
      continue;
    }
    if (node.type === "ERROR" || node.isMissing) {
      return true;
    }
    for (const child of node.namedChildren) {
      stack.push(child);
    }
  }
  return false;
}

function artifactFallback(
  filePath: string,
  language: string,
  content: string,
): CodeChunk[] {
  const lineCount = content.length === 0 ? 1 : content.split(/\r?\n/).length;
  return [
    {
      filePath,
      language,
      kind: "artifact",
      name: basename(filePath),
      startLine: 1,
      endLine: lineCount,
      content,
    },
  ];
}

function assertNoSiblingOverlap(chunks: CodeChunk[]): void {
  const byKind = new Map<string, CodeChunk[]>();
  for (const chunk of chunks) {
    if (chunk.kind === "method") {
      continue;
    }
    const list = byKind.get(chunk.kind) ?? [];
    list.push(chunk);
    byKind.set(chunk.kind, list);
  }
  for (const list of byKind.values()) {
    const sorted = [...list].sort((a, b) => a.startLine - b.startLine);
    for (let i = 1; i < sorted.length; i += 1) {
      const prev = sorted[i - 1]!;
      const current = sorted[i]!;
      if (current.startLine <= prev.endLine) {
        throw new Error(
          `overlapping ${prev.kind} chunks in ${prev.filePath}: ${prev.name} vs ${current.name}`,
        );
      }
    }
  }
}

/** Parse a TypeScript or JavaScript source file into semantic {@link CodeChunk}s. */
export async function chunkTypeScriptFile(
  filePath: string,
  content: string,
): Promise<CodeChunk[]> {
  const language = detectLanguage(filePath);
  const publicLanguage =
    language === "tsx" || language === "typescript" ? "typescript" : "javascript";

  try {
    const parser = await getParser(language);
    const tree = parser.parse(content);
    if (!tree || hasParseErrors(tree)) {
      return artifactFallback(filePath, publicLanguage, content);
    }

    const root = tree.rootNode;
    const { importChunks, importSpecifiers } = collectImportChunks(
      filePath,
      publicLanguage,
      root,
      content,
    );
    const symbolChunks: CodeChunk[] = [];
    walkDeclarations(filePath, publicLanguage, root, content, symbolChunks);
    const moduleChunk = buildModuleChunk(
      filePath,
      publicLanguage,
      content,
      importSpecifiers,
      tree,
    );

    const chunks = [moduleChunk, ...importChunks, ...symbolChunks];
    assertNoSiblingOverlap(chunks);
    return chunks;
  } catch {
    return artifactFallback(filePath, publicLanguage, content);
  }
}