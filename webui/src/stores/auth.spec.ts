import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Principal } from '../api/types';

// The store re-exports a module-level reactive, so every case resets it and the
// fetch seam rather than re-importing the module.
const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));
vi.mock('../api/client', () => ({ api: apiMock }));

const {
  authStore,
  detectMode,
  displayName,
  hasScope,
  hasScopes,
  login,
  logout,
} = await import('./auth');

const STORAGE_KEY = 'libref.api_key';

function principal(overrides: Partial<Principal> = {}): Principal {
  return {
    id: 1,
    name: 'dev-operator',
    kind: 'human',
    tenant_id: 'default',
    roles: ['developer'],
    scopes: ['read', 'feedback:write'],
    ...overrides,
  };
}

function reset(): void {
  authStore.key = '';
  authStore.me = null;
  authStore.mode = 'unknown';
  authStore.ready = false;
  localStorage.clear();
}

beforeEach(() => {
  reset();
});

describe('detectMode', () => {
  it('replays a stored key instead of probing anonymously', async () => {
    authStore.key = 'libref_stored';
    apiMock.mockResolvedValue({ data: principal(), requestId: 'r1' });

    await detectMode();

    expect(authStore.mode).toBe('enforced');
    expect(authStore.me?.name).toBe('dev-operator');
    expect(authStore.ready).toBe(true);
    // Exactly one call, and it carried credentials.
    expect(apiMock).toHaveBeenCalledTimes(1);
    expect(apiMock.mock.calls[0][1]).toEqual({});
  });

  it('falls back to dev mode when a stale key fails but the server is open', async () => {
    authStore.key = 'libref_stale';
    apiMock
      .mockRejectedValueOnce(new Error('401'))
      .mockResolvedValueOnce({ data: principal({ roles: ['platform_admin'] }), requestId: 'r2' });

    await detectMode();

    expect(authStore.mode).toBe('dev');
    expect(authStore.me?.roles).toEqual(['platform_admin']);
    expect(apiMock).toHaveBeenCalledTimes(2);
    expect(apiMock.mock.calls[1][1]).toEqual({ auth: false });
  });

  it('reports enforced + anonymous when no key is stored and the probe fails', async () => {
    apiMock.mockRejectedValue(new Error('401'));

    await detectMode();

    expect(authStore.mode).toBe('enforced');
    expect(authStore.me).toBeNull();
    expect(authStore.ready).toBe(true);
  });

  it('never re-probes once resolved', async () => {
    authStore.ready = true;
    await detectMode();
    expect(apiMock).not.toHaveBeenCalled();
  });
});

describe('login and logout', () => {
  it('trims the key, persists it, and adopts the resolved principal', async () => {
    apiMock.mockResolvedValue({ data: principal({ name: 'ops' }), requestId: 'r3' });

    const me = await login('  libref_abc  ');

    expect(me.name).toBe('ops');
    expect(authStore.key).toBe('libref_abc');
    expect(authStore.mode).toBe('enforced');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('libref_abc');
  });

  it('clears state and storage on logout', async () => {
    authStore.key = 'libref_abc';
    authStore.me = principal();
    authStore.mode = 'enforced';
    localStorage.setItem(STORAGE_KEY, 'libref_abc');

    logout();

    expect(authStore.key).toBe('');
    expect(authStore.me).toBeNull();
    expect(authStore.ready).toBe(false);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe('scope gates', () => {
  it('denies everything without a principal', () => {
    expect(hasScope('read')).toBe(false);
    expect(hasScopes(['read'])).toBe(false);
  });

  it('requires every scope for hasScopes but any single one for hasScope', () => {
    authStore.me = principal({ scopes: ['read', 'review:read'] });
    expect(hasScope('review:read')).toBe(true);
    expect(hasScope('review:write')).toBe(false);
    expect(hasScopes(['read', 'review:read'])).toBe(true);
    expect(hasScopes(['read', 'review:write'])).toBe(false);
    expect(hasScopes([])).toBe(true);
  });

  it('treats an empty scope list as read-only', () => {
    authStore.me = principal({ scopes: [] });
    expect(hasScope('read')).toBe(false);
    expect(hasScopes([])).toBe(true);
  });
});

describe('displayName', () => {
  it('prefers the principal name, then the dev label, then the anonymous label', () => {
    authStore.me = principal({ name: 'alice' });
    expect(displayName()).toBe('alice');

    authStore.me = principal({ name: '' });
    expect(displayName()).toBe('operator');

    authStore.me = null;
    authStore.mode = 'dev';
    expect(typeof displayName()).toBe('string');

    authStore.mode = 'enforced';
    expect(typeof displayName()).toBe('string');
  });
});
