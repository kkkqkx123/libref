import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DocInsightView from './DocInsightView.vue';
import { authStore } from '../stores/auth';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/insights/doc', component: { template: '<div />' } }],
  });
  await router.push(
    '/insights/doc?library=demo&version=1.0&ecosystem=custom&view=links',
  );
  await router.isReady();
  const wrapper = mount(DocInsightView, { global: { plugins: [router] } });
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
  apiMock.mockImplementation(() =>
    Promise.resolve(
      {
        data: {
          items: [{ document_id: 7, api: 'demo.Client.connect', feature_nodes: ['demo'] }],
          degraded: false,
          count: 1,
          view: 'links',
        },
        requestId: 'r1',
      },
    ),
  );
});

describe('DocInsightView', () => {
  it('queries the links view with a block id filter', async () => {
    const wrapper = await mountView();
    const number = wrapper.find('input[type=number]');
    await number.setValue('7');
    await wrapper.find('button').trigger('click');
    await flushPromises();

    const calls = apiMock.mock.calls.filter((call) => call[0] === '/doc-insight');
    expect(calls.length).toBeGreaterThan(0);
    const params = calls[calls.length - 1][1]?.params;
    expect(params.library).toBe('demo');
    expect(params.version).toBe('1.0');
    expect(params.document_id).toBe(7);
    expect(params.view).toBe('links');
    expect(wrapper.text()).toContain('demo.Client.connect');
  });
});
