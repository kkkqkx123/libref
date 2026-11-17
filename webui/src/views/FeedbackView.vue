<template>
  <div class="libref-page">
    <PageHeader title="反馈" description="提交挖掘问题，处理人标记解决" />
    <div v-if="canSubmit" class="libref-card">
      <h4>提交反馈</h4>
      <el-form :model="form" label-width="90px" @submit.prevent="submit">
        <el-form-item label="实体类型">
          <el-input v-model="form.entity_type" style="width: 220px" />
        </el-form-item>
        <el-form-item label="实体标识">
          <el-input-number v-model="form.entity_id" :min="0" />
        </el-form-item>
        <el-form-item label="种类">
          <el-input v-model="form.kind" style="width: 220px" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="form.body" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" :disabled="!form.body.trim()" @click="submit">
            提交
          </el-button>
        </el-form-item>
      </el-form>
    </div>
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent="reload">
        <el-form-item label="状态">
          <el-select v-model="status" clearable placeholder="全部" style="width: 160px" @change="reload">
            <el-option label="待处理" value="open" />
            <el-option label="已确认" value="acknowledged" />
            <el-option label="已解决" value="resolved" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button @click="reload">刷新</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading && items.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="items.length === 0" description="暂无反馈" />
      <div v-for="item in items" :key="item.id" class="libref-card">
        <div class="libref-feedback-head">
          <StatusBadge :status="item.status" />
          <span class="libref-mono">{{ item.entity_type }}#{{ item.entity_id }}</span>
          <span class="libref-muted">{{ item.kind }}</span>
          <span class="libref-muted">{{ item.created_at || '' }}</span>
          <el-button
            v-if="canResolve && item.status !== 'resolved'"
            link
            type="primary"
            size="small"
            @click="resolve(item)"
          >
            标记解决
          </el-button>
        </div>
        <p>{{ item.body }}</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElInputNumber, ElOption, ElSelect } from 'element-plus';
import { reactive, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, FeedbackItem } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import StatusBadge from '../components/common/StatusBadge.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';

const route = useRoute();
const canSubmit = hasScope('feedback:write');
const canResolve = hasScope('feedback:resolve');

const form = reactive({
  entity_type: typeof route.query.entity_type === 'string' ? route.query.entity_type : 'call_instance',
  entity_id: 0,
  kind: 'general',
  body: typeof route.query.body === 'string' ? route.query.body : '',
});
const status = ref('');
const loading = ref(false);
const submitting = ref(false);
const error = ref('');
const requestId = ref('');
const items = ref<FeedbackItem[]>([]);

async function submit(): Promise<void> {
  if (!form.body.trim()) return;
  submitting.value = true;
  try {
    await api<{ id: number; ok: boolean }>('/feedback', {
      method: 'POST',
      body: {
        entity_type: form.entity_type || 'call_instance',
        entity_id: form.entity_id,
        kind: form.kind || 'general',
        body: form.body.trim(),
      },
    });
    notifySuccess('反馈已提交');
    form.body = '';
    await reload();
  } catch (err) {
    notifyError(err, '提交失败');
  } finally {
    submitting.value = false;
  }
}

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<FeedbackItem>>('/feedback', {
      params: { limit: 50, status: status.value || undefined },
    });
    items.value = data.items;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载失败';
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

async function resolve(item: FeedbackItem): Promise<void> {
  try {
    await api<{ ok: boolean }>(`/feedback/${item.id}/resolve`, { method: 'POST' });
    notifySuccess('已标记解决');
    item.status = 'resolved';
  } catch (err) {
    notifyError(err, '操作失败');
  }
}

void reload();
</script>

<style scoped>
.libref-feedback-head {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
