<template>
  <el-dialog v-model="visible" title="签发 API 密钥" width="480px">
    <el-form :model="form" label-width="90px">
      <el-form-item label="名称">
        <el-input v-model="form.name" placeholder="如 frontend-admin" />
      </el-form-item>
      <el-form-item label="租户">
        <el-input v-model="form.tenant_id" placeholder="default" />
      </el-form-item>
      <el-form-item label="类型">
        <el-select v-model="form.kind">
          <el-option label="人工" value="human" />
          <el-option label="机器" value="machine" />
        </el-select>
      </el-form-item>
      <el-form-item label="角色">
        <TagInput v-model="form.roles" placeholder="如 developer，回车添加" />
      </el-form-item>
      <el-form-item label="有效期">
        <el-date-picker
          v-model="expiresDate"
          type="datetime"
          placeholder="不选则永久有效"
          style="width: 100%"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :disabled="!form.name" @click="submit">签发</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ElButton, ElDatePicker, ElDialog, ElForm, ElFormItem, ElInput, ElOption, ElSelect } from 'element-plus';
import { reactive, ref } from 'vue';
import { api } from '../../api/client';
import { notifyError, notifySuccess } from '../../stores/notify';
import TagInput from '../common/TagInput.vue';

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
    notifySuccess('密钥已签发，原文仅展示一次');
    emit('created', data.key);
    visible.value = false;
    form.name = '';
    form.roles = [];
    expiresDate.value = null;
  } catch (error) {
    notifyError(error, '签发失败');
  }
}
</script>
