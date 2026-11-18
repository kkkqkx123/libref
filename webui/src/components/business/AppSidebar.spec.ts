import { mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it } from 'vitest';
import AppSidebar from './AppSidebar.vue';
import { messages } from '../../i18n';
import { NAV_ROUTES } from '../../router/nav';
import { authStore } from '../../stores/auth';

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      ...NAV_ROUTES.map((entry) => ({
        ...entry.route,
        meta: { titleKey: entry.titleKey },
      })),
    ],
  });
}

async function mountWith(scopes: string[]) {
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
  const router = makeRouter();
  await router.push('/');
  await router.isReady();
  return mount(AppSidebar, { global: { plugins: [router] } });
}

const nav = () => messages.value.nav;

beforeEach(() => {
  authStore.key = '';
  authStore.me = null;
  authStore.mode = 'unknown';
  authStore.ready = false;
});

describe('AppSidebar', () => {
  it('shows nothing to an anonymous visitor', async () => {
    const wrapper = await mountWith([]);
    expect(wrapper.text()).not.toContain(nav().catalog);
    expect(wrapper.findAll('.el-menu-item')).toHaveLength(0);
  });

  it('gives a developer the retrieval and request sections only', async () => {
    const wrapper = await mountWith([
      'read',
      'feedback:write',
      'feedback:read:own',
      'index:request',
    ]);
    const text = wrapper.text();
    expect(text).toContain(nav().catalog);
    expect(text).toContain(nav().requests);
    expect(text).toContain(nav().feedback);
    expect(text).not.toContain(nav().review);
    expect(text).not.toContain(nav().admin);
  });

  it('gives a review administrator the governance pages it owns', async () => {
    const wrapper = await mountWith(['read', 'review:read', 'feedback:read']);
    const text = wrapper.text();
    expect(text).toContain(nav().review);
    expect(text).toContain(nav().feedback);
    expect(text).not.toContain(nav().admin);
    expect(text).not.toContain(nav().ingest);
  });

  it('shows the admin entry to a caller holding only one of its scopes', async () => {
    // Regression: the route used any-scope while the sidebar required all of
    // them, so a config-only administrator lost the menu entry entirely.
    expect((await mountWith(['read', 'admin:config'])).text()).toContain(nav().admin);
    expect((await mountWith(['read', 'admin:keys'])).text()).toContain(nav().admin);
  });

  it('shows every section to a platform administrator', async () => {
    const wrapper = await mountWith([
      'read',
      'review:read',
      'feedback:read',
      'admin:keys',
      'admin:config',
      'ingest:write',
      'index:request',
    ]);
    const text = wrapper.text();
    for (const title of [nav().catalog, nav().apiInsight, nav().ingest, nav().admin]) {
      expect(text).toContain(title);
    }
  });

  it('keeps the reader page out of the sidebar', async () => {
    const wrapper = await mountWith(['read']);
    expect(wrapper.text()).not.toContain(nav().reader);
  });
});
