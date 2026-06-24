/**
 * Simulates a fresh consumer: npm pack → install tarball → run --version.
 * Does not hit the npm registry (plan 26 pre-publish gate).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  unlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const cliRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

const pack = spawnSync("npm", ["pack", "--silent"], {
  cwd: cliRoot,
  encoding: "utf8",
  shell: true,
});
assert.equal(pack.status, 0, pack.stderr || pack.stdout);

const tarball = readdirSync(cliRoot).find((f) => f.endsWith(".tgz"));
assert.ok(tarball, "npm pack did not create a tarball");

const scratch = mkdtempSync(join(tmpdir(), "provena-pack-install-"));
try {
  spawnSync("npm", ["init", "-y"], { cwd: scratch, encoding: "utf8", shell: true });

  const install = spawnSync(
    "npm",
    ["install", join(cliRoot, tarball)],
    { cwd: scratch, encoding: "utf8", shell: true },
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);

  const pkgJson = JSON.parse(
    readFileSync(join(scratch, "package.json"), "utf8"),
  );
  assert.ok(
    pkgJson.dependencies?.["@provena/cli"] || pkgJson.devDependencies?.["@provena/cli"],
    "tarball install did not add @provena/cli",
  );

  const cliBin = join(scratch, "node_modules", "@provena/cli", "dist", "cli.js");
  const versionRun = spawnSync(process.execPath, [cliBin, "--version"], {
    cwd: scratch,
    encoding: "utf8",
  });
  assert.equal(versionRun.status, 0, versionRun.stderr);
  assert.match(versionRun.stdout.trim(), /^0\.1\.\d+$/);

  const helpRun = spawnSync(process.execPath, [cliBin, "--help"], {
    cwd: scratch,
    encoding: "utf8",
  });
  assert.equal(helpRun.status, 0, helpRun.stderr);
  for (const cmd of ["init", "index", "search"]) {
    assert.ok(helpRun.stdout.includes(cmd), `help missing ${cmd}`);
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
  unlinkSync(join(cliRoot, tarball));
}

console.log("pack-install.test: ok");