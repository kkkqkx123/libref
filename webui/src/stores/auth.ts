// Authentication state: API key storage, mode detection, scope gates.
// Unknown roles fall back to read-only, matching the backend default.
import { reactive } from 'vue';
import { messages } from '../i18n';
import { api } from '../api/client';
import type { Principal } from '../api/types';

const STORAGE_KEY = 'libref.api_key';

interface AuthState {
  key: string;
  me: Principal | null;
  mode: 'unknown' | 'dev' | 'enforced';
  ready: boolean;
}

export const authStore = reactive<AuthState>({
  key: localStorage.getItem(STORAGE_KEY) || '',
  me: null,
  mode: 'unknown',
  ready: false,
});

export function hasScope(scope: string): boolean {
  return (authStore.me?.scopes || []).includes(scope);
}

export function hasScopes(scopes: string[]): boolean {
  return scopes.every((scope) => hasScope(scope));
}

export async function detectMode(): Promise<void> {
  if (authStore.ready) return;
  // A stored key must be replayed first: probing without it would report
  // "enforced + anonymous" on every reload and drop a live session.
  if (authStore.key) {
    try {
      const { data } = await api<Principal>('/me');
      authStore.me = data;
      authStore.mode = 'enforced';
      authStore.ready = true;
      return;
    } catch {
      // Stale key or dev-mode server; the anonymous probe below decides which.
    }
  }
  try {
    const { data } = await api<Principal>('/me', { auth: false });
    authStore.me = data;
    authStore.mode = 'dev';
  } catch {
    authStore.me = null;
    authStore.mode = 'enforced';
  } finally {
    authStore.ready = true;
  }
}

export async function login(key: string): Promise<Principal> {
  const trimmed = key.trim();
  authStore.key = trimmed;
  const { data } = await api<Principal>('/me');
  authStore.me = data;
  authStore.mode = 'enforced';
  authStore.ready = true;
  localStorage.setItem(STORAGE_KEY, trimmed);
  return data;
}

export function logout(): void {
  authStore.key = '';
  authStore.me = null;
  authStore.ready = false;
  localStorage.removeItem(STORAGE_KEY);
}

export function displayName(): string {
  if (authStore.me) return authStore.me.name || 'operator';
  return authStore.mode === 'dev' ? messages.value.app.devOperator : messages.value.app.notLoggedIn;
}
