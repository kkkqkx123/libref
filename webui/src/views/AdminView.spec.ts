import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AdminView from './AdminView.vue';
import { messages } from '../i18n';
import { authStore } from '../stores/auth';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

const principals = [
  {
    id: 1,
    tenant_id: 'default',
    kind: 'human',
    name: 'legacy',
    status: 'active',
    roles: ['kb_admin', 'sys_admin'],
    scopes: ['read', 'admin:keys', 'review:write'],
    conflicting: [['kb_admin', 'sys_admin']],
  },
  {
    id: 2,
    tenant_id: 'default',
    kind: 'machine',
    name: 'assistant',
    status: 'active',
    roles: ['external'],
    scopes: ['read', 'index:request'],
    conflicting: [],
  },
];

function signIn(scopes: string[]) {
  authStore.mode = 'enforced';
  authStore.key = 'libref_test';
  authStore.me = {
    id: 1,
    name: 'tester',
    kind: 'human',
    tenant_id: 'default',
    roles: [],
    scopes,
  };
  authStore.ready = true;
}

function routesFor(url: string): unknown {
  if (url === '/admin/keys') return { data: { items: [] }, requestId: 'r1' };
  if (url === '/admin/principals') {
    return { data: { items: principals }, requestId: 'r2' };
  }
  return { data: {}, requestId: 'r3' };
}

async function mountView(scopes: string[]) {
  signIn(scopes);
  const wrapper = mount(AdminView);
  await flushPromises();
  return wrapper;
}

function tabByLabel(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll('.el-tabs__item').find((tab) => tab.text() === label);
}

function requested(url: string) {
  return apiMock.mock.calls.filter((call) => call[0] === url);
}

beforeEach(() => {
  authStore.key = '';
  authStore.me = null;
  authStore.mode = 'unknown';
  authStore.ready = false;
  apiMock.mockReset();
  apiMock.mockImplementation((url: string) =>
    Promise.resolve(routesFor(url)),
  );
});

describe('AdminView', () => {
  it('keeps the principal view away from config-only admins', async () => {
    const wrapper = await mountView(['read', 'admin:config']);

    expect(tabByLabel(wrapper, messages.value.admin.principalsTab)).toBeUndefined();
    expect(requested('/admin/principals')).toHaveLength(0);
    expect(requested('/admin/keys')).toHaveLength(0);
  });

  it('lists principals with conflicts flagged once the tab opens', async () => {
    const wrapper = await mountView(['read', 'admin:keys']);

    const tab = tabByLabel(wrapper, messages.value.admin.principalsTab);
    expect(tab).toBeDefined();
    tab!.trigger('click');
    await flushPromises();

    expect(requested('/admin/principals')).toHaveLength(1);
    const text = wrapper.text();
    expect(text).toContain('legacy');
    expect(text).toContain('assistant');
    expect(text).toContain('kb_admin + sys_admin');
    expect(text).toContain(messages.value.admin.separationHint(1));
  });

  it('shows the healthy marker when no principal conflicts', async () => {
    apiMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/admin/principals'
          ? { data: { items: [principals[1]] }, requestId: 'r2' }
          : routesFor(url),
      ),
    );
    const wrapper = await mountView(['read', 'admin:keys']);
    tabByLabel(wrapper, messages.value.admin.principalsTab)!.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain(messages.value.admin.separationOk);
    expect(wrapper.text()).not.toContain(messages.value.admin.separationHint(1));
  });
});
