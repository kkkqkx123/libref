// Route guard as a pure function of (target, auth state).
//
// Kept out of `router/index.ts` so the decision table can be tested without
// building a router, and so the same rules are reused by any future entry.
import type { RouteLocationNormalized } from 'vue-router';
import { authStore, hasScopes } from '../stores/auth';

export interface GuardRedirect {
  path: string;
  query?: Record<string, string>;
}

type GuardTarget = Pick<RouteLocationNormalized, 'meta' | 'fullPath'>;

export function authGuard(to: GuardTarget): true | GuardRedirect {
  if (authStore.mode === 'enforced' && !authStore.key && !to.meta.public) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  const scopes = (to.meta.scopes as string[] | undefined) || [];
  if (scopes.length === 0) return true;
  const held = authStore.me?.scopes || [];
  const allowed =
    to.meta.any === true
      ? scopes.some((scope) => held.includes(scope))
      : hasScopes(scopes);
  if (allowed) return true;
  // A caller that is authenticated but under-scoped gets an explanation; an
  // unidentified one is sent to the login page instead, so no dead end.
  if (authStore.me) return { path: '/forbidden' };
  return { path: '/login', query: { redirect: to.fullPath } };
}
