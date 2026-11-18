import { mount } from '@vue/test-utils';
import { defineComponent, nextTick } from 'vue';
import { beforeEach, describe, expect, it } from 'vitest';
import KeyIssueDialog from './KeyIssueDialog.vue';
import { messages } from '../../i18n';
import { authStore } from '../../stores/auth';

// element-plus is stubbed down to the surface this dialog actually drives: the
// role picker (two-way binding) and the submit button (disabled state).
const ElSelectStub = defineComponent({
  name: 'ElSelect',
  // Loosely typed on purpose: the same stub also stands in for the non-array
  // `kind` picker, which the dialog renders beside the role picker.
  props: { modelValue: { default: undefined } },
  emits: ['update:modelValue'],
  template: '<div class="select-stub"><slot /></div>',
});

const stubs = {
  Teleport: true,
  ElDialog: { template: '<div><slot /><slot name="footer" /></div>' },
  ElForm: { template: '<form><slot /></form>' },
  ElFormItem: { template: '<div><slot /></div>' },
  ElInput: true,
  ElInputNumber: true,
  ElDatePicker: true,
  ElOption: true,
  ElSelect: ElSelectStub,
  ElButton: {
    props: { disabled: { type: Boolean, default: false } },
    template: '<button :disabled="disabled"><slot /></button>',
  },
};

function signIn(roles: string[]) {
  authStore.mode = 'enforced';
  authStore.key = 'libref_test';
  authStore.me = {
    id: 1,
    name: 'tester',
    kind: 'human',
    tenant_id: 'default',
    roles,
    scopes: [],
  };
  authStore.ready = true;
}

function mountDialog() {
  return mount(KeyIssueDialog, {
    props: { modelValue: true },
    global: { stubs },
  });
}

function submitButton(wrapper: ReturnType<typeof mountDialog>) {
  // The footer holds cancel first, then the primary action.
  return wrapper.find('button[type="primary"]');
}

// The dialog holds two selects (kind and roles); the role one carries the test
// id so the assertions cannot bind to the wrong instance.
function roleSelect(wrapper: ReturnType<typeof mountDialog>) {
  const found = wrapper
    .findAllComponents(ElSelectStub)
    .find((item) => item.attributes('data-testid') === 'role-select');
  if (!found) throw new Error('role select not rendered');
  return found;
}

beforeEach(() => {
  authStore.key = '';
  authStore.me = null;
  authStore.mode = 'unknown';
  authStore.ready = false;
});

describe('KeyIssueDialog', () => {
  it('states the separation rule before any role is picked', () => {
    signIn(['platform_admin']);
    const wrapper = mountDialog();
    expect(wrapper.text()).toContain(messages.value.admin.roleSeparationHint);
    expect(wrapper.text()).not.toContain('cannot');
  });

  it('blocks submission until a name is entered', () => {
    signIn(['platform_admin']);
    expect(submitButton(mountDialog()).attributes('disabled')).toBeDefined();
  });

  it('warns and stays blocked when conflicting roles are selected', async () => {
    signIn(['platform_admin']);
    const wrapper = mountDialog();

    roleSelect(wrapper).vm.$emit('update:modelValue', [
      'kb_admin',
      'sys_admin',
    ]);
    await nextTick();

    const text = wrapper.text();
    expect(text).toContain(
      messages.value.admin.roleConflict('kb_admin + sys_admin'),
    );
    expect(text).not.toContain(messages.value.admin.roleSeparationHint);
    expect(submitButton(wrapper).attributes('disabled')).toBeDefined();
  });

  it('clears the warning once the combination is valid again', async () => {
    signIn(['platform_admin']);
    const wrapper = mountDialog();
    const select = roleSelect(wrapper);

    select.vm.$emit('update:modelValue', ['developer', 'kb_admin']);
    await nextTick();
    expect(wrapper.text()).toContain(messages.value.admin.roleConflict('developer + kb_admin'));

    select.vm.$emit('update:modelValue', ['kb_admin']);
    await nextTick();
    expect(wrapper.text()).not.toContain(messages.value.admin.roleConflict('developer + kb_admin'));
    expect(wrapper.text()).toContain(messages.value.admin.roleSeparationHint);
  });

  it('only offers roles the caller can grant', () => {
    signIn(['sys_admin']);
    const wrapper = mountDialog();
    const labels = roleSelect(wrapper)
      .findAllComponents({ name: 'ElOption' })
      .map((option) => option.props('label'));
    expect(labels).toEqual(['sys_admin']);
  });

  it('offers every role to a platform administrator', () => {
    signIn(['platform_admin']);
    const labels = roleSelect(mountDialog())
      .findAllComponents({ name: 'ElOption' })
      .map((option) => option.props('label'));
    expect(labels).toContain('developer');
    expect(labels).toContain('platform_admin');
  });
});
