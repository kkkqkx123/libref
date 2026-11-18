import { beforeEach, describe, expect, it } from 'vitest';
import { authGuard } from './guard';
import { authStore } from '../stores/auth';

function target(meta: Record<string, unknown>, fullPath = '/target') {
  return { meta, fullPath };
}

function signIn(scopes: string[], key = 'libref_key', mode: 'dev' | 'enforced' = 'enforced') {
  authStore.mode = mode;
  authStore.key = key;
  authStore.me = {
    id: 1,
    name: 'tester',
    kind: 'human',
    tenant_id: 'default',
    roles: ['developer'],
    scopes,
  };
  authStore.ready = true;
}

beforeEach(() => {
  authStore.key = '';
  authStore.me = null;
  authStore.mode = 'unknown';
  authStore.ready = false;
});

describe('authGuard', () => {
  it('sends an anonymous caller to the login page with a return path', () => {
    authStore.mode = 'enforced';
    expect(authGuard(target({ titleKey: 'search' }, '/search?q=a'))).toEqual({
      path: '/login',
      query: { redirect: '/search?q=a' },
    });
  });

  it('lets an anonymous caller reach public pages', () => {
    authStore.mode = 'enforced';
    expect(authGuard(target({ public: true }, '/login'))).toBe(true);
  });

  it('does not require a key in dev mode', () => {
    signIn(['read'], '', 'dev');
    expect(authGuard(target({ titleKey: 'search' }))).toBe(true);
  });

  it('allows an authenticated caller on a route without scope requirements', () => {
    signIn(['read']);
    expect(authGuard(target({ titleKey: 'search' }))).toBe(true);
  });

  it('allows a caller holding every declared scope', () => {
    signIn(['read', 'review:read']);
    expect(authGuard(target({ scopes: ['read', 'review:read'] }))).toBe(true);
  });

  it('explains a missing scope to an authenticated caller instead of looping to login', () => {
    signIn(['read']);
    expect(authGuard(target({ scopes: ['review:read'] }))).toEqual({
      path: '/forbidden',
    });
  });

  it('accepts any single scope when the route sets any: true', () => {
    signIn(['admin:config']);
    expect(
      authGuard(target({ scopes: ['admin:keys', 'admin:config'], any: true })),
    ).toBe(true);
  });

  it('rejects a caller holding none of the alternatives', () => {
    signIn(['read']);
    expect(
      authGuard(target({ scopes: ['admin:keys', 'admin:config'], any: true })),
    ).toEqual({ path: '/forbidden' });
  });

  it('requires all scopes when any is unset, even if one matches', () => {
    signIn(['admin:config']);
    expect(authGuard(target({ scopes: ['admin:keys', 'admin:config'] }))).toEqual({
      path: '/forbidden',
    });
  });

  it('falls back to login when a scope-gated route is reached without a principal', () => {
    authStore.mode = 'dev';
    authStore.key = '';
    expect(authGuard(target({ scopes: ['read'] }, '/catalog'))).toEqual({
      path: '/login',
      query: { redirect: '/catalog' },
    });
  });

  it('treats an empty scope list as open to any authenticated caller', () => {
    signIn([]);
    expect(authGuard(target({ scopes: [] }))).toBe(true);
  });
});
