import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import FeedbackView from './FeedbackView.vue';
import { messages } from '../i18n';
import { authStore } from '../stores/auth';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

// Keep ApiError real: the view branches on `instanceof`.
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

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

async function mountView(scopes: string[]) {
  signIn(scopes);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/feedback', component: { template: '<div />' } }],
  });
  await router.push('/feedback');
  await router.isReady();
  const wrapper = mount(FeedbackView, { global: { plugins: [router] } });
  await flushPromises();
  return wrapper;
}

function listedCalls() {
  return apiMock.mock.calls.filter((call) => call[0] === '/feedback');
}

const listPayload = {
  data: {
    items: [
      {
        id: 7,
        entity_type: 'call_instance',
        entity_id: 3,
        kind: 'general',
        body: 'typo in the summary',
        status: 'open',
        created_at: '2026-01-01T00:00:00Z',
      },
    ],
  },
  requestId: 'r1',
};

beforeEach(() => {
  authStore.key = '';
  authStore.me = null;
  authStore.mode = 'unknown';
  authStore.ready = false;
  apiMock.mockReset();
  apiMock.mockResolvedValue(listPayload);
});

describe('FeedbackView', () => {
  it('loads the list for an author who may read their own rows', async () => {
    const wrapper = await mountView(['read', 'feedback:write', 'feedback:read:own']);

    expect(listedCalls()).toHaveLength(1);
    expect(wrapper.text()).toContain('typo in the summary');
    expect(wrapper.text()).toContain(messages.value.feedback.ownOnlyHint);
  });

  it('does not request a list it is not allowed to read', async () => {
    // Regression: submitting reloaded the list unconditionally, so a developer
    // saw a 403 that covered the success notice.
    const wrapper = await mountView(['read', 'feedback:write']);

    expect(listedCalls()).toHaveLength(0);
    expect(wrapper.text()).toContain(messages.value.feedback.submitTitle);
    expect(wrapper.text()).not.toContain('typo in the summary');
    expect(wrapper.text()).not.toContain(messages.value.feedback.ownOnlyHint);
  });

  it('shows a reviewer the tenant-wide list without the author hint', async () => {
    const wrapper = await mountView(['read', 'feedback:read', 'feedback:resolve']);

    expect(listedCalls()).toHaveLength(1);
    expect(wrapper.text()).toContain('typo in the summary');
    expect(wrapper.text()).not.toContain(messages.value.feedback.ownOnlyHint);
    expect(wrapper.text()).toContain(messages.value.feedback.resolve);
  });

  it('hides the resolve action from a reader without the scope', async () => {
    const wrapper = await mountView(['read', 'feedback:read']);

    expect(listedCalls()).toHaveLength(1);
    expect(wrapper.text()).not.toContain(messages.value.feedback.resolve);
  });

  it('offers the submit form only to a writer', async () => {
    const reader = await mountView(['read', 'feedback:read']);
    expect(reader.text()).not.toContain(messages.value.feedback.submitTitle);

    apiMock.mockResolvedValue(listPayload);
    const writer = await mountView(['read', 'feedback:write']);
    expect(writer.text()).toContain(messages.value.feedback.submitTitle);
    expect(writer.text()).not.toContain(messages.value.feedback.ownOnlyHint);
  });
});
