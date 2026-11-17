#!/usr/bin/env node
// Locks the message catalogue: both locales must expose the same keys, the same
// value kinds, the same interpolation arity, no empty strings, and no
// placeholder-style messages. Interface text outside the locale files must be
// free of CJK characters, so nothing bypasses the catalogue.
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { zhCN } from '../src/i18n/locales/zh-CN.ts';
import { en } from '../src/i18n/locales/en.ts';

const root = join(fileURLToPath(new URL('..', import.meta.url)), 'src');
const localesDir = join(root, 'i18n', 'locales');
const CATALOGS = { 'zh-CN': zhCN, en };

function walk(node, path, out) {
  for (const [key, value] of Object.entries(node)) {
    const where = path ? `${path}.${key}` : key;
    if (value && typeof value === 'object') {
      walk(value, where, out);
      continue;
    }
    out.set(where, value);
  }
  return out;
}

const flattened = Object.fromEntries(
  Object.entries(CATALOGS).map(([name, catalog]) => [name, walk(catalog, '', new Map())]),
);

const problems = [];
const [reference, ...others] = Object.keys(flattened);
const referenceKeys = new Set(flattened[reference].keys());

for (const [name, entries] of Object.entries(flattened)) {
  for (const key of referenceKeys) {
    if (!entries.has(key)) problems.push(`${name}: missing key ${key}`);
  }
  for (const key of entries.keys()) {
    if (!referenceKeys.has(key)) problems.push(`${name}: extra key ${key}`);
  }
}

for (const [name, entries] of Object.entries(flattened)) {
  for (const [key, value] of entries) {
    const expected = flattened[reference].get(key);
    if (expected === undefined) continue;
    if (typeof value !== typeof expected) {
      problems.push(`${name}: ${key} is ${typeof value}, expected ${typeof expected}`);
      continue;
    }
    if (typeof value === 'string') {
      if (!value.trim()) problems.push(`${name}: ${key} is empty`);
      if (/\{[a-zA-Z_][\w]*\}/.test(value)) {
        problems.push(`${name}: ${key} uses a placeholder string`);
      }
      continue;
    }
    if (typeof value === 'function') {
      if (value.length !== expected.length) {
        problems.push(`${name}: ${key} takes ${value.length} args, expected ${expected.length}`);
        continue;
      }
      const candidates = [
        Array(value.length).fill('sample'),
        Array(value.length).fill(1),
      ];
      const rendered = candidates.some((args) => {
        try {
          const sample = value(...args);
          return typeof sample === 'string' && sample.trim().length > 0;
        } catch {
          return false;
        }
      });
      if (!rendered) problems.push(`${name}: ${key} does not render a non-empty string`);
    }
  }
}

const CJK = /[\u4e00-\u9fff]/;
function scan(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (full === localesDir) continue;
    if (statSync(full).isDirectory()) {
      scan(full);
      continue;
    }
    if (!/\.(vue|ts)$/.test(entry) || entry.endsWith('.d.ts')) continue;
    const text = readFileSync(full, 'utf8');
    text.split('\n').forEach((line, index) => {
      if (CJK.test(line)) problems.push(`${full}:${index + 1} hardcodes CJK text`);
    });
  }
}
scan(root);

if (problems.length > 0) {
  console.error(`i18n check failed:\n${problems.join('\n')}`);
  process.exit(1);
}
console.log('i18n: ok');
