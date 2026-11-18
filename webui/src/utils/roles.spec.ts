import { describe, expect, it } from 'vitest';
import {
  PLATFORM_ROLE,
  ROLE_OPTIONS,
  ROLE_SEPARATION_GROUPS,
  canIssueKey,
  grantableRoles,
  roleConflicts,
} from './roles';

describe('roleConflicts', () => {
  it('accepts each functional role on its own', () => {
    for (const role of ['developer', 'kb_admin', 'sys_admin', 'external']) {
      expect(roleConflicts([role])).toEqual([]);
    }
  });

  it('rejects every pair inside a separation group', () => {
    const group = ROLE_SEPARATION_GROUPS[0];
    for (let i = 0; i < group.length; i += 1) {
      for (let j = i + 1; j < group.length; j += 1) {
        expect(roleConflicts([group[i], group[j]])).toEqual([[group[i], group[j]]]);
      }
    }
  });

  it('reports the whole group when all three are held', () => {
    const group = ROLE_SEPARATION_GROUPS[0];
    expect(roleConflicts([...group])).toEqual([group]);
  });

  it('lets the read-only consumer combine with a functional role', () => {
    expect(roleConflicts(['external', 'developer'])).toEqual([]);
    expect(roleConflicts(['external', 'kb_admin'])).toEqual([]);
  });

  it('exempts the platform role, which is not a tenant function', () => {
    expect(roleConflicts([PLATFORM_ROLE])).toEqual([]);
    expect(roleConflicts([PLATFORM_ROLE, 'developer', 'kb_admin'])).toEqual([]);
  });

  it('ignores duplicates and an empty list', () => {
    expect(roleConflicts([])).toEqual([]);
    expect(roleConflicts(['developer', 'developer'])).toEqual([]);
  });
});

describe('grantableRoles', () => {
  it('offers every role to a platform administrator', () => {
    expect(grantableRoles([PLATFORM_ROLE]).map((option) => option.value)).toEqual(
      ROLE_OPTIONS.map((option) => option.value),
    );
  });

  it('offers a tenant administrator only what it already holds', () => {
    expect(grantableRoles(['sys_admin']).map((option) => option.value)).toEqual([
      'sys_admin',
    ]);
    expect(grantableRoles(['developer']).map((option) => option.value)).toEqual([
      'developer',
    ]);
  });

  it('never leaks the platform role to a tenant administrator', () => {
    const values = grantableRoles(['sys_admin', 'kb_admin']).map((o) => o.value);
    expect(values).not.toContain(PLATFORM_ROLE);
  });

  it('offers nothing to an unknown role list', () => {
    expect(grantableRoles([])).toEqual([]);
  });
});

describe('canIssueKey', () => {
  it('requires a name', () => {
    expect(canIssueKey('', ['developer'])).toBe(false);
    expect(canIssueKey('   ', ['developer'])).toBe(false);
    expect(canIssueKey('ops', ['developer'])).toBe(true);
  });

  it('blocks a conflicting combination before the request is sent', () => {
    expect(canIssueKey('ops', ['kb_admin', 'sys_admin'])).toBe(false);
    expect(canIssueKey('ops', ['kb_admin', 'external'])).toBe(true);
  });
});
