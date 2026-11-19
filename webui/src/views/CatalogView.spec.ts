import { flushPromises, mount } from '@vue/test-utils';
import { createMemoryHistory, createRouter } from 'vue-router';
import { defineComponent, h, inject, provide, type InjectionKey } from 'vue';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import CatalogView from './CatalogView.vue';
import { ApiError } from '../api/client';
import { messages } from '../i18n';
import { authStore } from '../stores/auth';

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: apiMock };
});

const rowKey = Symbol('catalog-row') as InjectionKey<Record<string, unknown>>;

const RowStub = defineComponent({
  name: 'CatalogRowStub',
  props: { row: { type: Object as () => Record<string, unknown>, required: true } },
  setup(props, { slots, attrs }) {
    provide(rowKey, props.row);
    return () => h('div', { ...attrs, class: 'row-stub' }, slots.default?.() ?? []);
  },
});

const ElTableStub = defineComponent({
  name: 'ElTable',
  props: { data: { type: Array as () => Record<string, unknown>[], default: () => [] } },
  emits: ['row-click'],
  setup(props, { slots, emit }) {
    return () =>
      h(
        'div',
        { class: 'table-stub' },
        props.data.map((row) =>
          h(
            RowStub,
            { row, onClick: () => emit('row-click', row) },
            () => slots.default?.({ row }) ?? [],
          ),
        ),
      );
  },
});

const ElTableColumnStub = defineComponent({
  name: 'ElTableColumn',
  props: { prop: String, label: String },
  setup(props, { slots }) {
    const row = inject(rowKey, {});
    return () =>
      h(
        'div',
        { class: 'cell-stub' },
        slots.default ? slots.default({ row }) : String(row[props.prop ?? ''] ?? ''),
      );
  },
});

const ElDialogStub = defineComponent({
  name: 'ElDialog',
  props: { modelValue: Boolean, title: String },
  setup(props, { slots }) {
    return () =>
      props.modelValue
        ? h('div', { class: 'dialog-stub', 'data-title': props.title }, slots.default?.() ?? [])
        : null;
  },
});

const stubs = {
  ElTable: ElTableStub,
  ElTableColumn: ElTableColumnStub,
  ElDialog: ElDialogStub,
};

const libraries = [
  { id: 1, name: 'shared-name', ecosystem: 'npm', versions: 1 },
  { id: 2, name: 'shared-name', ecosystem: 'pypi', versions: 1 },
];

const versions: Record<string, Record<string, unknown>[]> = {
  npm: [{ id: 10, version: '1.0.0', ecosystem: 'npm', pages: 0, registry_confirmed: true }],
  pypi: [{ id: 20, version: '2.0.0', ecosystem: 'pypi', pages: 0, registry_confirmed: true }],
};

function envelope(items: Record<string, unknown>[]) {
  return { data: { items, degraded: false, count: items.length }, requestId: 'r1' };
}

let depsResponse: Record<string, unknown>;
let depsFailure: ApiError | null;

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/catalog', component: { template: '<div />' } }],
  });
  await router.push('/catalog');
  await router.isReady();
  const wrapper = mount(CatalogView, { global: { plugins: [router], stubs } });
  await flushPromises();
  return wrapper;
}

async function openDepsDialog(wrapper: Awaited<ReturnType<typeof mountView>>, ecosystem: string) {
  const row = wrapper.findAll('.row-stub').find((item) => item.text().includes(ecosystem));
  expect(row).toBeDefined();
  await row!.trigger('click');
  await flushPromises();

  const button = wrapper
    .findAll('button')
    .find((item) => item.text() === messages.value.catalog.viewDeps);
  expect(button).toBeDefined();
  await button!.trigger('click');
  await flushPromises();
  return wrapper.find('.dialog-stub');
}

function depsCalls() {
  return apiMock.mock.calls.filter((call) => call[0] === '/repo-deps');
}

beforeEach(() => {
  authStore.mode = 'enforced';
  authStore.ready = true;
  authStore.me = {
    id: 1, name: 'tester', kind: 'human', tenant_id: 'default', roles: [], scopes: ['read'],
  };
  depsResponse = envelope([]);
  depsFailure = null;
  apiMock.mockReset();
  apiMock.mockImplementation(
    (url: string, options?: { params?: Record<string, string | number | undefined> }) => {
      if (url === '/catalog') {
        if (!options?.params?.library) return Promise.resolve(envelope(libraries));
        const ecosystem = String(options.params.ecosystem ?? '');
        return Promise.resolve(envelope(versions[ecosystem] ?? []));
      }
      if (url === '/repo-deps') {
        return depsFailure
          ? Promise.reject(depsFailure)
          : Promise.resolve(depsResponse);
      }
      return Promise.resolve({ data: {}, requestId: 'r1' });
    },
  );
});

describe('CatalogView dependency dialog', () => {
  it('uses the clicked library ecosystem when loading dependencies', async () => {
    const wrapper = await mountView();
    const dialog = await openDepsDialog(wrapper, 'pypi');

    expect(depsCalls()).toHaveLength(1);
    expect(depsCalls()[0][1].params).toMatchObject({
      library: 'shared-name',
      version: '2.0.0',
      ecosystem: 'pypi',
    });
    expect(dialog.attributes('data-title')).toContain('shared-name');
  });

  it('shows the empty state when the version has no declared dependencies', async () => {
    const wrapper = await mountView();
    const dialog = await openDepsDialog(wrapper, 'npm');

    expect(depsCalls()).toHaveLength(1);
    expect(dialog.text()).toContain(messages.value.catalog.depsEmpty);
  });

  it('surfaces the backend request id on a missing version', async () => {
    depsFailure = new ApiError(
      404,
      'http_404',
      messages.value.common.notFound,
      'page not found',
      'req-404',
    );
    const wrapper = await mountView();
    const dialog = await openDepsDialog(wrapper, 'npm');

    expect(dialog.text()).toContain('req-404');
  });
});
