#!/usr/bin/env node
// Regenerates TypeScript bindings from a running backend. The checked-in
// src/api/types.ts stays hand-maintained; this script refreshes the raw schema
// snapshot that codegen consumes.
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';

const base = process.env.LIBREF_BASE_URL || 'http://127.0.0.1:8001';
const dir = fileURLToPath(new URL('..', import.meta.url));

async function main() {
  const response = await fetch(`${base}/openapi.json`);
  if (!response.ok) {
    throw new Error(`backend is not reachable at ${base}: ${response.status}`);
  }
  const document = await response.json();
  writeFileSync(join(dir, 'openapi.snapshot.json'), JSON.stringify(document, null, 2));
  execSync('npx openapi-typescript openapi.snapshot.json -o src/api/schema.d.ts', {
    cwd: dir,
    stdio: 'inherit',
  });
  console.log('api bindings: ok');
}

main().catch((error) => {
  console.error(`gen:api failed: ${error.message}`);
  process.exit(1);
});
