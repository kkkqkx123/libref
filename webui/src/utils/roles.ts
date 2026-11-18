// Role catalogue mirrored from the service (`services/infra/auth.py`).
//
// This table exists only to keep the issue dialog honest: it offers the roles
// the caller may actually grant and blocks a combination the server would
// reject. The server stays authoritative -- `create_principal` re-validates
// every role list, so a drifted table here can only cause an avoidable 400,
// never an over-grant.
export const PLATFORM_ROLE = 'platform_admin';

export interface RoleOption {
  value: string;
  scopes: string[];
}

export const ROLE_OPTIONS: RoleOption[] = [
  { value: 'external', scopes: ['read', 'index:request'] },
  {
    value: 'developer',
    scopes: ['read', 'feedback:write', 'feedback:read:own', 'index:request'],
  },
  {
    value: 'kb_admin',
    scopes: [
      'read',
      'review:read',
      'review:write',
      'feedback:read',
      'feedback:resolve',
      'index:request',
      'index:approve',
    ],
  },
  {
    value: 'sys_admin',
    scopes: [
      'read',
      'ingest:write',
      'jobs:write',
      'admin:keys',
      'admin:config',
      'index:request',
      'index:approve',
    ],
  },
  { value: PLATFORM_ROLE, scopes: ['*'] },
];

// Same contract as the server: at most one role per group, and the platform
// role is exempt because it is not a tenant function.
export const ROLE_SEPARATION_GROUPS: string[][] = [
  ['developer', 'kb_admin', 'sys_admin'],
];

export function roleConflicts(roles: string[]): string[][] {
  const held = new Set(roles);
  if (held.has(PLATFORM_ROLE)) return [];
  return ROLE_SEPARATION_GROUPS.map((group) =>
    group.filter((role) => held.has(role)),
  ).filter((shared) => shared.length > 1);
}

export function grantableRoles(callerRoles: string[]): RoleOption[] {
  const held = new Set(callerRoles);
  if (held.has(PLATFORM_ROLE)) return ROLE_OPTIONS;
  return ROLE_OPTIONS.filter(
    (option) => option.value !== PLATFORM_ROLE && held.has(option.value),
  );
}

// A submission needs a name and a combination the server will accept; keeping
// the predicate here lets the dialog stay a pure view.
export function canIssueKey(name: string, roles: string[]): boolean {
  return name.trim().length > 0 && roleConflicts(roles).length === 0;
}
