import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import JobsView from './JobsView.vue';
import { authStore } from '../stores/auth';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/jobs', component: { template: '<div />' } }],
  });
  await router.push('/jobs');
  await router.isReady();
  const wrapper = mount(JobsView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  authStore.mode = 'enforced';
  authStore.ready = true;
  authStore.me = {
    id: 1, name: 'tester', kind: 'human', tenant_id: 'default', roles: [], scopes: ['read', 'jobs:write'],
  };
  apiMock.mockReset();
  apiMock.mockImplementation((url: string) =>
    Promise.resolve(
      url === '/jobs' ? { data: { jobs: [] }, requestId: 'r1' } : { data: {}, requestId: 'r1' },
    ),
  );
});

describe('JobsView smoke', () => {
  it('loads the job list on mount', async () => {
    await mountView();
    const calls = apiMock.mock.calls.filter((call) => call[0] === '/jobs');
    expect(calls.length).toBeGreaterThan(0);
  });
});
