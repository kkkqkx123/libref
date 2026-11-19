#!/usr/bin/env node
// Regenerates TypeScript bindings from the backend's OpenAPI document. Both
// outputs are versioned so a contract change is visible in review; --check
// regenerates into a temporary directory and compares against the checked-in
// files instead of rewriting them.
//
// The snapshot is produced offline by tools/export_openapi.py, which builds the
// app object directly. A running backend is only a fallback, because requiring
// one meant the snapshot was never generated at all.
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = fileURLToPath(new URL('..', import.meta.url));
const repo = fileURLToPath(new URL('../..', import.meta.url));
const snapshot = join(dir, 'openapi.snapshot.json');
const schema = join(dir, 'src/api/schema.d.ts');
const base = process.env.LIBREF_BASE_URL || 'http://127.0.0.1:8001';
const check = process.argv.includes('--check');

function pythonCandidates() {
  return [
    process.env.LIBREF_PYTHON,
    join(repo, '.venv', 'bin', 'python'),
    join(repo, '.venv', 'Scripts', 'python.exe'),
    'python3',
    'python',
  ].filter(Boolean);
}

function exportOffline(out) {
  for (const python of pythonCandidates()) {
    const result = spawnSync(
      python,
      [join(repo, 'tools', 'export_openapi.py'), '--out', out],
      { cwd: repo, stdio: 'inherit' },
    );
    if (result.status === 0) return true;
  }
  return false;
}

async function exportOnline(out) {
  console.warn(`offline export unavailable, falling back to ${base}`);
  const response = await fetch(`${base}/openapi.json`);
  if (!response.ok) {
    throw new Error(`backend is not reachable at ${base}: ${response.status}`);
  }
  const document = await response.json();
  writeFileSync(out, `${JSON.stringify(document, null, 2)}\n`);
}

function generateTypes(snapshotPath, schemaPath) {
  const result = spawnSync(
    'npx',
    ['--no-install', 'openapi-typescript', snapshotPath, '-o', schemaPath],
    { cwd: dir, stdio: 'inherit', shell: process.platform === 'win32' },
  );
  if (result.status !== 0) {
    throw new Error('openapi-typescript failed; run npm install in webui/ first');
  }
}

function assertSame(generated, checkedIn, label) {
  let expected;
  try {
    expected = readFileSync(checkedIn, 'utf8');
  } catch {
    throw new Error(`${label} is missing; run npm run gen:api`);
  }
  if (readFileSync(generated, 'utf8') !== expected) {
    throw new Error(`${label} is stale; run npm run gen:api`);
  }
}

async function main() {
  const temp = check ? mkdtempSync(join(tmpdir(), 'libref-api-')) : null;
  try {
    const snapshotPath = temp ? join(temp, 'openapi.snapshot.json') : snapshot;
    const schemaPath = temp ? join(temp, 'schema.d.ts') : schema;
    if (!(await exportOffline(snapshotPath))) await exportOnline(snapshotPath);
    generateTypes(snapshotPath, schemaPath);
    if (temp) {
      assertSame(snapshotPath, snapshot, 'openapi.snapshot.json');
      assertSame(schemaPath, schema, 'src/api/schema.d.ts');
      console.log('api bindings: up to date');
    } else {
      console.log('api bindings: ok');
    }
  } finally {
    if (temp) rmSync(temp, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`gen:api failed: ${error.message}`);
  process.exit(1);
});
