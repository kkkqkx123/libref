import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ReaderView from './ReaderView.vue';
import { authStore } from '../stores/auth';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/reader', component: { template: '<div />' } }],
  });
  await router.push('/reader?url=https%3A%2F%2Fexample.com%2Fdocs&mode=page');
  await router.isReady();
  const wrapper = mount(ReaderView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  authStore.mode = 'enforced';
  authStore.ready = true;
  authStore.me = {
    id: 1, name: 'tester', kind: 'human', tenant_id: 'default', roles: [], scopes: ['read'],
  };
  apiMock.mockReset();
  apiMock.mockImplementation((url: string) =>
    Promise.resolve(
      url === '/catalog'
        ? { data: { items: [] }, requestId: 'r1' }
        : url === '/read'
          ? {
              data: {
                url: 'https://example.com/docs', title: 'Docs', path: '/docs',
                library: 'lib', version: '1.0', content: 'body', chunks: 0,
                chunk_details: [], docs: [],
              },
              requestId: 'r1',
            }
          : { data: {}, requestId: 'r1' },
    ),
  );
});

describe('ReaderView smoke', () => {
  it('loads the page from a deep link with url and mode', async () => {
    await mountView();
    const calls = apiMock.mock.calls.filter((call) => call[0] === '/read');
    expect(calls.length).toBeGreaterThan(0);
    const params = calls[calls.length - 1][1]?.params;
    expect(params.url).toBe('https://example.com/docs');
    expect(params.mode).toBe('page');
  });
});
