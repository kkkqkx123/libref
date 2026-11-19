import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import IngestWizardView from './IngestWizardView.vue';
import { messages } from '../i18n';
import { authStore } from '../stores/auth';

const { apiMock, pushMock } = vi.hoisted(() => ({ apiMock: vi.fn(), pushMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-router')>();
  return { ...actual, useRouter: () => ({ push: pushMock }) };
});

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/ingest', component: { template: '<div />' } }],
  });
  await router.push('/ingest');
  await router.isReady();
  const wrapper = mount(IngestWizardView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  authStore.mode = 'enforced';
  authStore.ready = true;
  authStore.me = {
    id: 1, name: 'tester', kind: 'human', tenant_id: 'default', roles: [], scopes: ['ingest:write'],
  };
  apiMock.mockReset();
  pushMock.mockReset();
  apiMock.mockImplementation((url: string) =>
    Promise.resolve(
      url === '/resolve'
        ? { data: { name: 'org/repo', repo_url: 'https://github.com/org/repo' }, requestId: 'r1' }
        : url === '/catalog'
          ? { data: { items: [], kind: 'libraries' }, requestId: 'r1' }
          : { data: {}, requestId: 'r1' },
    ),
  );
});

describe('IngestWizardView smoke', () => {
  it('redirects a resolved repository to index requests instead of scraping it', async () => {
    const wrapper = await mountView();
    const nameInput = wrapper
      .findAll('input')
      .find((el) => el.attributes('placeholder') === messages.value.ingest.namePlaceholder);
    await nameInput!.setValue('org/repo');
    const resolveButton = wrapper.findAll('button').find((el) => el.text() === messages.value.common.parse);
    await resolveButton!.trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain(messages.value.ingest.repoRedirectHint);
    expect(apiMock.mock.calls.some((call) => call[0] === '/scrape')).toBe(false);

    const goButton = wrapper.findAll('button').find((el) => el.text() === messages.value.ingest.goRequests);
    await goButton!.trigger('click');
    expect(pushMock).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/requests', query: expect.objectContaining({ kind: 'repo' }) }),
    );
  });
});
