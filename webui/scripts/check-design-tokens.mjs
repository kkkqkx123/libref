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

// Type scale and spacing tokens are layout invariants; they need not differ
// between themes, but must be declared once in :root.
const REQUIRED_LIGHT_ONLY = [
  '--fs-xs', '--fs-sm', '--fs-base', '--fs-lg', '--fs-xl',
  '--sp-1', '--sp-1-5', '--sp-2', '--sp-3', '--sp-4', '--sp-4-5', '--sp-5', '--sp-10',
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
for (const name of REQUIRED_LIGHT_ONLY) {
  if (!light.has(name)) throw new Error(`token ${name} missing in :root`);
}
// Element Plus mapping must stay in the token file.
if (!css.includes('--el-color-primary: var(--brand)')) {
  throw new Error('Element Plus primary mapping missing in tokens.css');
}

const HEX = /#[0-9a-fA-F]{3,8}\b/g;
// Spacing/type declarations must use tokens. Width/height/min-width/max-width
// are layout sizes and stay as literal px (Element Plus controls vary), and
// 1px borders are a convention, not a spacing token.
const SPACING_DECL = /\b(font-size|margin|margin-top|margin-bottom|margin-left|margin-right|padding|padding-top|padding-bottom|padding-left|padding-right|gap|row-gap|column-gap)\s*:\s*([^;]+);/g;
const INLINE_STYLE = /\sstyle="[^"]*"/;

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      walk(full);
      continue;
    }
    if (!/\.(vue|ts)$/.test(entry) || entry.endsWith('.d.ts') || entry.endsWith('.spec.ts')) continue;
    const text = readFileSync(full, 'utf8');
    if (/\.ts$/.test(entry)) {
      // TS files: colours only (no templates/styles there).
      const stripped = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
      const hits = stripped.match(HEX);
      if (hits) throw new Error(`${full} uses raw color values: ${hits.join(', ')}`);
      continue;
    }
    const stripped = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
    const hits = stripped.match(HEX);
    if (hits) throw new Error(`${full} uses raw color values: ${hits.join(', ')}`);

    if (INLINE_STYLE.test(text)) {
      throw new Error(`${full} uses inline style; use a token-backed utility class`);
    }
    for (const m of stripped.matchAll(SPACING_DECL)) {
      const value = m[2].trim();
      // A token-backed value contains var(--...); numeric px without it is drift.
      if (/\d+px/.test(value) && !/var\(--/.test(value)) {
        throw new Error(`${full} hardcodes ${m[1]}: ${value}; use a spacing/type token`);
      }
      // Guard against a bad bulk rewrite that left the property name as its own
      // value (e.g. `margin: margin`), which is invalid CSS yet parses cleanly.
      if (/^[a-z-]+$/i.test(value) && value !== 'auto' && value !== 'normal' && value !== 'inherit') {
        throw new Error(`${full} has malformed ${m[1]}: ${value}`);
      }
    }
  }
}
walk(root);

console.log('design tokens: ok');
