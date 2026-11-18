import { describe, expect, it } from 'vitest';
import router from './index';
import { NAV_ROUTES, type NavGroupKey } from './nav';

const GROUPS: NavGroupKey[] = ['retrieval', 'insights', 'ingestTasks', 'governance'];

describe('navigation table', () => {
  it('keeps paths unique', () => {
    const paths = NAV_ROUTES.map((entry) => entry.route.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('declares at least one scope for every entry', () => {
    for (const entry of NAV_ROUTES) {
      expect(entry.scopes.length, entry.route.path).toBeGreaterThan(0);
    }
  });

  it('only groups entries under known sections', () => {
    for (const entry of NAV_ROUTES) {
      if (entry.group === undefined) continue;
      expect(GROUPS).toContain(entry.group);
    }
  });

  it('marks pages whose own tabs differ in permission as any-scope', () => {
    // `/admin` splits keys and config, `/feedback` splits read and write; a
    // caller holding either half must still see the page.
    for (const path of ['/admin', '/feedback']) {
      const entry = NAV_ROUTES.find((item) => item.route.path === path);
      expect(entry, path).toBeDefined();
      expect(entry?.any, path).toBe(true);
      expect(entry?.scopes.length, path).toBeGreaterThan(1);
    }
  });

  it('is the single source of the router metadata', () => {
    const registered = router.getRoutes();
    for (const entry of NAV_ROUTES) {
      const record = registered.find((item) => item.path === entry.route.path);
      expect(record, entry.route.path).toBeDefined();
      expect(record?.meta.titleKey, entry.route.path).toBe(entry.titleKey);
      expect(record?.meta.scopes, entry.route.path).toEqual(entry.scopes);
      expect(record?.meta.any, entry.route.path).toBe(entry.any === true);
    }
  });

  it('keeps every non-navigable page reachable', () => {
    const paths = router.getRoutes().map((item) => item.path);
    expect(paths).toContain('/login');
    expect(paths).toContain('/forbidden');
    expect(paths).toContain('/reader');
  });
});
