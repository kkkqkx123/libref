#!/usr/bin/env node
// Regenerates TypeScript bindings from the OpenAPI document. The checked-in
// src/api/types.ts stays hand-maintained; this script refreshes the raw schema
// snapshot that codegen consumes.
//
// The snapshot is produced offline by tools/export_openapi.py, which builds the
// app object directly. A running backend is only a fallback, because requiring
// one meant the snapshot was never generated at all.
import { spawnSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = fileURLToPath(new URL('..', import.meta.url));
const repo = fileURLToPath(new URL('../..', import.meta.url));
const snapshot = join(dir, 'openapi.snapshot.json');
const base = process.env.LIBREF_BASE_URL || 'http://127.0.0.1:8001';

function pythonCandidates() {
  return [
    process.env.LIBREF_PYTHON,
    join(repo, '.venv', 'bin', 'python'),
    join(repo, '.venv', 'Scripts', 'python.exe'),
    'python3',
    'python',
  ].filter(Boolean);
}

// Returns true when the offline exporter wrote the snapshot.
function exportOffline() {
  for (const python of pythonCandidates()) {
    const result = spawnSync(
      python,
      [join(repo, 'tools', 'export_openapi.py'), '--out', snapshot],
      { cwd: repo, stdio: 'inherit' },
    );
    if (result.status === 0) return true;
  }
  return false;
}

async function exportOnline() {
  console.warn(`offline export unavailable, falling back to ${base}`);
  const response = await fetch(`${base}/openapi.json`);
  if (!response.ok) {
    throw new Error(`backend is not reachable at ${base}: ${response.status}`);
  }
  const document = await response.json();
  writeFileSync(snapshot, `${JSON.stringify(document, null, 2)}\n`);
}

async function main() {
  if (!exportOffline()) await exportOnline();
  const result = spawnSync(
    'npx',
    ['--no-install', 'openapi-typescript', snapshot, '-o', 'src/api/schema.d.ts'],
    { cwd: dir, stdio: 'inherit', shell: process.platform === 'win32' },
  );
  if (result.status !== 0) {
    throw new Error('openapi-typescript failed; run npm install in webui/ first');
  }
  console.log('api bindings: ok');
}

main().catch((error) => {
  console.error(`gen:api failed: ${error.message}`);
  process.exit(1);
});
