// URL state helpers: pages mirror their effective filters into the address bar
// so a copied link reproduces the same view, and hydrate them back on mount.
import { useRoute, useRouter } from 'vue-router';
import type { LocationQuery, LocationQueryValue } from 'vue-router';
import { RESULT_LIMIT_MAX, RESULT_LIMIT_MIN, scopeStore } from '../stores/scope';

export type QueryValue = string | number | boolean | null | undefined;
export type QuerySnapshot = Record<string, QueryValue | string[]>;

type RawQueryValue = LocationQueryValue | LocationQueryValue[] | undefined;

function single(value: RawQueryValue): string {
  if (Array.isArray(value)) return value[0] || '';
  return value || '';
}

export function readString(value: RawQueryValue): string {
  return single(value).trim();
}

export function readNumber(value: RawQueryValue, fallback: number): number {
  const parsed = Number(single(value));
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function readBool(value: RawQueryValue, fallback: boolean): boolean {
  const raw = single(value).toLowerCase();
  if (raw === '1' || raw === 'true') return true;
  if (raw === '0' || raw === 'false') return false;
  return fallback;
}

export function readList(value: RawQueryValue): string[] {
  return single(value)
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function encode(value: QueryValue | string[]): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (Array.isArray(value)) return value.length > 0 ? value.join(',') : undefined;
  if (typeof value === 'boolean') return value ? '1' : '0';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

/** Mirrors filter state into the current route without pushing history entries. */
export function useQuerySync() {
  const route = useRoute();
  const router = useRouter();

  function sync(snapshot: QuerySnapshot): void {
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(route.query)) {
      const encoded = encode(value as QueryValue);
      if (encoded !== undefined) next[key] = encoded;
    }
    for (const [key, value] of Object.entries(snapshot)) {
      const encoded = encode(value);
      if (encoded === undefined) delete next[key];
      else next[key] = encoded;
    }
    const target = router.resolve({ path: route.path, query: next }).fullPath;
    if (target !== route.fullPath) void router.replace(target);
  }

  return { sync };
}

/** Global library/version/ecosystem hints carried by a shared link. */
export function applyScopeFromQuery(query: LocationQuery): void {
  const library = readString(query.library);
  const version = readString(query.version);
  const ecosystem = readString(query.ecosystem);
  if (library) scopeStore.library = library;
  if (version) scopeStore.version = version;
  if (ecosystem) scopeStore.ecosystem = ecosystem;
  if (query.limit !== undefined) {
    const parsed = readNumber(query.limit, scopeStore.resultLimit);
    scopeStore.resultLimit = Math.min(RESULT_LIMIT_MAX, Math.max(RESULT_LIMIT_MIN, parsed));
  }
}

export function scopeSnapshot(): QuerySnapshot {
  return {
    library: scopeStore.library || undefined,
    version: scopeStore.version || undefined,
    ecosystem: scopeStore.ecosystem || undefined,
  };
}
