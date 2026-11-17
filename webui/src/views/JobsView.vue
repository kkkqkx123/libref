<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.jobs" :description="messages.jobs.description">
      <template #actions>
        <el-button v-if="canCreate" type="primary" @click="createOpen = true">{{ messages.jobs.createTask }}</el-button>
      </template>
    </PageHeader>
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent="reload">
        <el-form-item :label="messages.common.limit">
          <el-input-number v-model="limit" :min="1" :max="200" @change="reload" />
        </el-form-item>
        <el-form-item>
          <el-button @click="reload">{{ messages.common.refresh }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading && jobs.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="jobs.length === 0" :description="messages.jobs.empty" />
      <el-table v-else :data="jobs" stripe style="width: 100%" @row-click="openDetail">
        <el-table-column prop="id" :label="messages.jobs.id" width="90" />
        <el-table-column prop="kind" :label="messages.jobs.kind" width="160" />
        <el-table-column prop="status" :label="messages.jobs.status" width="120">
          <template #default="scope"><StatusBadge :status="scope.row.status" /></template>
        </el-table-column>
        <el-table-column prop="finished_at" :label="messages.jobs.finishedAt" width="200" />
        <el-table-column prop="error" :label="messages.jobs.errorSummary" show-overflow-tooltip />
      </el-table>
    </template>

    <el-drawer v-model="detailOpen"  :title="messages.jobs.detailTitle" size="480px" @closed="stopPolling">
      <LoadingPanel v-if="detailLoading" />
      <template v-else-if="detail">
        <KeyValue :entries="detailEntries" />
        <h4>{{ messages.jobs.result }}</h4>
        <pre class="libref-mono libref-result-pre">{{ detailText }}</pre>
        <div class="libref-detail-actions">
          <el-button
            v-if="canCreate && (detail.status === 'pending' || detail.status === 'running')"
            type="danger"
            @click="cancel(detail.id)"
          >
            {{ messages.jobs.cancelTask }}
          </el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="createOpen"  :title="messages.jobs.createTitle" width="520px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item :label="messages.common.kind">
          <el-input v-model="createForm.kind"  :placeholder="messages.jobs.kindPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.jobs.params">
          <el-input v-model="createForm.paramsText" type="textarea" :rows="4"  :placeholder="messages.jobs.paramsPlaceholder" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">{{ messages.common.cancel }}</el-button>
        <el-button type="primary" :disabled="!createForm.kind" @click="create">{{ messages.common.submit }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElDialog, ElDrawer, ElForm, ElFormItem, ElInput, ElInputNumber, ElTable, ElTableColumn } from 'element-plus';
import { ElMessageBox } from 'element-plus';
import { computed, onUnmounted, reactive, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { JobDetail, JobSummary } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import StatusBadge from '../components/common/StatusBadge.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';

const route = useRoute();
const canCreate = hasScope('jobs:write');
const limit = ref(20);
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const jobs = ref<JobSummary[]>([]);
const detailOpen = ref(false);
const detailLoading = ref(false);
const detail = ref<JobDetail | null>(null);
const createOpen = ref(false);
const createForm = reactive({ kind: '', paramsText: '{}' });
let timer: number | null = null;

const detailEntries = computed<KeyValueEntry[]>(() => {
  if (!detail.value) return [];
  return [
    { label: messages.value.jobs.id, value: String(detail.value.id) },
    { label: messages.value.jobs.kind, value: detail.value.kind },
    { label: messages.value.jobs.status, value: detail.value.status },
    { label: messages.value.jobs.finishedAt, value: detail.value.finished_at || '' },
    { label: messages.value.jobs.error, value: detail.value.error || '' },
  ];
});

const detailText = computed(() => {
  if (!detail.value || detail.value.result === undefined) return messages.value.jobs.none;
  try {
    return JSON.stringify(detail.value.result, null, 2);
  } catch {
    return String(detail.value.result);
  }
});

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<{ jobs: JobSummary[] }>('/jobs', { params: { limit: limit.value } });
    jobs.value = data.jobs;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

async function loadDetail(id: number): Promise<void> {
  detailLoading.value = true;
  try {
    const { data } = await api<JobDetail>(`/jobs/${id}`);
    detail.value = data;
    if (data.status === 'pending' || data.status === 'running') startPolling(id);
    else stopPolling();
  } catch (err) {
    notifyError(err, messages.value.jobs.loadDetailFailed);
  } finally {
    detailLoading.value = false;
  }
}

function openDetail(row: JobSummary): void {
  detail.value = null;
  detailOpen.value = true;
  void loadDetail(row.id);
}

function startPolling(id: number): void {
  stopPolling();
  timer = window.setInterval(() => {
    void loadDetail(id);
  }, 3000);
}

function stopPolling(): void {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

async function cancel(id: number): Promise<void> {
  try {
    await ElMessageBox.confirm(messages.value.jobs.cancelConfirm, messages.value.jobs.cancelTask, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await api<{ ok: boolean }>(`/jobs/${id}/cancel`, { method: 'POST' });
    notifySuccess(messages.value.jobs.cancelled);
    await loadDetail(id);
    await reload();
  } catch (err) {
    notifyError(err, messages.value.jobs.cancelFailed);
  }
}

async function create(): Promise<void> {
  let params: Record<string, unknown> = {};
  try {
    params = JSON.parse(createForm.paramsText || '{}') as Record<string, unknown>;
  } catch {
    notifyError(new Error(messages.value.jobs.invalidJson), messages.value.common.paramError);
    return;
  }
  try {
    const { data } = await api<{ id: number; ok: boolean }>('/jobs', {
      method: 'POST',
      body: { kind: createForm.kind, params },
    });
    notifySuccess(messages.value.jobs.submitted(data.id));
    createOpen.value = false;
    createForm.kind = '';
    createForm.paramsText = '{}';
    await reload();
  } catch (err) {
    notifyError(err, messages.value.jobs.submitFailed);
  }
}

onUnmounted(() => stopPolling());

void reload().then(() => {
  const id = route.query.id;
  if (typeof id === 'string' && id) {
    detailOpen.value = true;
    void loadDetail(Number(id));
  }
});
</script>

<style scoped>
.libref-result-pre {
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--surface-bg);
  border-radius: var(--radius-small);
  padding: 12px;
}
.libref-detail-actions {
  margin-top: 16px;
}
</style>
