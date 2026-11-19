import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SearchView from './SearchView.vue';
import { authStore } from '../stores/auth';
import { scopeStore } from '../stores/scope';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/search', component: { template: '<div />' } }],
  });
  await router.push('/search');
  await router.isReady();
  const wrapper = mount(SearchView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  authStore.mode = 'enforced';
  authStore.ready = true;
  authStore.me = {
    id: 1, name: 'tester', kind: 'human', tenant_id: 'default', roles: [], scopes: ['read'],
  };
  scopeStore.includePrerelease = false;
  apiMock.mockReset();
  apiMock.mockImplementation((url: string) =>
    Promise.resolve(
      url === '/catalog'
        ? { data: { items: [], kind: 'libraries' }, requestId: 'r1' }
        : url === '/search'
          ? { data: { items: [] }, requestId: 'r1' }
          : { data: {}, requestId: 'r1' },
    ),
  );
});

describe('SearchView smoke', () => {
  it('issues a search with the query and the prerelease flag', async () => {
    scopeStore.includePrerelease = true;
    const wrapper = await mountView();
    await wrapper.find('input').setValue('requests');
    await wrapper.find('button').trigger('click');
    await flushPromises();

    const calls = apiMock.mock.calls.filter((call) => call[0] === '/search');
    expect(calls.length).toBeGreaterThan(0);
    const params = calls[calls.length - 1][1]?.params;
    expect(params.query).toBe('requests');
    expect(params.include_prerelease).toBe(true);
  });
});
