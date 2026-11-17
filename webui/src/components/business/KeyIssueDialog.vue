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
        <TagInput v-model="form.roles"  :placeholder="messages.admin.rolesPlaceholder" />
      </el-form-item>
      <el-form-item :label="messages.admin.expires">
        <el-date-picker
          v-model="expiresDate"
          type="datetime"
           :placeholder="messages.admin.expiresPlaceholder"
          style="width: 100%"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ messages.common.cancel }}</el-button>
      <el-button type="primary" :disabled="!form.name" @click="submit">{{ messages.common.issue }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ElButton, ElDatePicker, ElDialog, ElForm, ElFormItem, ElInput, ElOption, ElSelect } from 'element-plus';
import { reactive, ref } from 'vue';
import { api } from '../../api/client';
import { notifyError, notifySuccess } from '../../stores/notify';
import TagInput from '../common/TagInput.vue';
import { messages } from '../../i18n';

const visible = defineModel<boolean>({ default: false });
const emit = defineEmits<{ created: [key: string] }>();

const form = reactive({ name: '', tenant_id: 'default', kind: 'human', roles: [] as string[] });
const expiresDate = ref<Date | null>(null);

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
