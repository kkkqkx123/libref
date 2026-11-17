#!/usr/bin/env node
// Locks the design token catalogue: required variables must exist in both
// themes, and pages/components must reference tokens instead of raw values.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(fileURLToPath(new URL('..', import.meta.url)), 'src');
const css = readFileSync(join(root, 'styles', 'tokens.css'), 'utf8');

const REQUIRED = [
  '--brand',
  '--semantic-info',
  '--semantic-success',
  '--semantic-warning',
  '--semantic-error',
  '--confidence-extracted',
  '--confidence-inferred',
  '--confidence-ambiguous',
  '--surface-bg',
  '--surface-card',
  '--text-primary',
  '--text-secondary',
  '--border-default',
];

function block(pattern) {
  const match = css.match(pattern);
  if (!match) throw new Error(`missing CSS block for ${pattern}`);
  return match[1];
}

function declared(text) {
  const out = new Set();
  for (const match of text.matchAll(/(--[\w-]+)\s*:/g)) out.add(match[1]);
  return out;
}

const light = declared(block(/:root\s*\{([\s\S]*?)\n\}/));
const dark = declared(block(/\.dark\s*\{([\s\S]*?)\n\}/));
for (const name of REQUIRED) {
  if (!light.has(name)) throw new Error(`token ${name} missing in :root`);
  if (!dark.has(name)) throw new Error(`token ${name} missing in .dark`);
}
// Element Plus mapping must stay in the token file.
if (!css.includes('--el-color-primary: var(--brand)')) {
  throw new Error('Element Plus primary mapping missing in tokens.css');
}

const HEX = /#[0-9a-fA-F]{3,8}\b/g;
function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      walk(full);
      continue;
    }
    if (!/\.(vue|ts)$/.test(entry) || entry.endsWith('.d.ts')) continue;
    const text = readFileSync(full, 'utf8');
    const stripped = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
    const hits = stripped.match(HEX);
    if (hits) throw new Error(`${full} uses raw color values: ${hits.join(', ')}`);
  }
}
walk(join(root, 'components'));
walk(join(root, 'views'));

console.log('design tokens: ok');
