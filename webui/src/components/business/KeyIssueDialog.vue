<template>
  <el-dialog v-model="visible"  :title="messages.admin.issueKey" width="480px">
    <el-form :model="form" label-width="90px">
      <el-form-item :label="messages.common.name">
        <el-input v-model="form.name"  :placeholder="messages.admin.namePlaceholder" />
      </el-form-item>
      <el-form-item :label="messages.admin.tenant">
        <el-input v-model="form.tenant_id"  placeholder="default" />
      </el-form-item>
      <el-form-item :label="messages.common.kind">
        <el-select v-model="form.kind">
          <el-option :label="messages.common.human" value="human" />
          <el-option :label="messages.common.machine" value="machine" />
        </el-select>
      </el-form-item>
      <el-form-item :label="messages.admin.roles">
        <el-select
          v-model="form.roles"
          multiple
          filterable
          data-testid="role-select" class="libref-w-full">
          <el-option
            v-for="role in roleOptions"
            :key="role.value"
            :label="role.value"
            :value="role.value"
          />
        </el-select>
        <div v-if="conflicts.length > 0" class="role-conflict">
          {{ messages.admin.roleConflict(conflicts[0].join(' + ')) }}
        </div>
        <div v-else class="role-hint">{{ messages.admin.roleSeparationHint }}</div>
      </el-form-item>
      <el-form-item :label="messages.admin.expires">
        <el-date-picker
          v-model="expiresDate"
          type="datetime"
           :placeholder="messages.admin.expiresPlaceholder" class="libref-w-full"/>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ messages.common.cancel }}</el-button>
      <el-button type="primary" :disabled="!canSubmit" @click="submit">{{ messages.common.issue }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ElButton, ElDatePicker, ElDialog, ElForm, ElFormItem, ElInput, ElOption, ElSelect } from 'element-plus';
import { computed, reactive, ref } from 'vue';
import { api } from '../../api/client';
import { notifyError, notifySuccess } from '../../stores/notify';
import { authStore } from '../../stores/auth';
import { canIssueKey, grantableRoles, roleConflicts } from '../../utils/roles';
import { messages } from '../../i18n';

const visible = defineModel<boolean>({ default: false });
const emit = defineEmits<{ created: [key: string] }>();

const form = reactive({ name: '', tenant_id: 'default', kind: 'human', roles: [] as string[] });
const expiresDate = ref<Date | null>(null);

// Offer only what this caller can actually grant; the server re-checks it.
const roleOptions = computed(() => grantableRoles(authStore.me?.roles || []));
const conflicts = computed(() => roleConflicts(form.roles));
const canSubmit = computed(() => canIssueKey(form.name, form.roles));

async function submit(): Promise<void> {
  try {
    const { data } = await api<{ id: number; key: string; prefix: string }>('/admin/keys', {
      method: 'POST',
      body: {
        name: form.name,
        tenant_id: form.tenant_id || 'default',
        kind: form.kind,
        roles: form.roles,
        scopes: [],
        expires_at: expiresDate.value ? expiresDate.value.getTime() / 1000 : null,
      },
    });
    notifySuccess(messages.value.admin.keyIssued);
    emit('created', data.key);
    visible.value = false;
    form.name = '';
    form.roles = [];
    expiresDate.value = null;
  } catch (error) {
    notifyError(error, messages.value.admin.issueFailed);
  }
}
</script>

<style scoped>
.role-conflict {
  margin-top: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--semantic-error);
}

.role-hint {
  margin-top: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
</style>
