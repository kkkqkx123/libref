<template>
  <div class="libref-page">
    <PageHeader title="任务中心" description="采集与通用任务的提交、跟踪与取消">
      <template #actions>
        <el-button v-if="canCreate" type="primary" @click="createOpen = true">新建任务</el-button>
      </template>
    </PageHeader>
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent="reload">
        <el-form-item label="条数">
          <el-input-number v-model="limit" :min="1" :max="200" @change="reload" />
        </el-form-item>
        <el-form-item>
          <el-button @click="reload">刷新</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading && jobs.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="jobs.length === 0" description="暂无任务，可从入库向导提交" />
      <el-table v-else :data="jobs" stripe style="width: 100%" @row-click="openDetail">
        <el-table-column prop="id" label="标识" width="90" />
        <el-table-column prop="kind" label="类型" width="160" />
        <el-table-column prop="status" label="状态" width="120">
          <template #default="scope"><StatusBadge :status="scope.row.status" /></template>
        </el-table-column>
        <el-table-column prop="finished_at" label="结束时间" width="200" />
        <el-table-column prop="error" label="错误摘要" show-overflow-tooltip />
      </el-table>
    </template>

    <el-drawer v-model="detailOpen" title="任务详情" size="480px" @closed="stopPolling">
      <LoadingPanel v-if="detailLoading" />
      <template v-else-if="detail">
        <KeyValue :entries="detailEntries" />
        <h4>结果</h4>
        <pre class="libref-mono libref-result-pre">{{ detailText }}</pre>
        <div class="libref-detail-actions">
          <el-button
            v-if="canCreate && (detail.status === 'pending' || detail.status === 'running')"
            type="danger"
            @click="cancel(detail.id)"
          >
            取消任务
          </el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="createOpen" title="新建通用任务" width="520px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item label="类型">
          <el-input v-model="createForm.kind" placeholder="任务类型" />
        </el-form-item>
        <el-form-item label="参数">
          <el-input v-model="createForm.paramsText" type="textarea" :rows="4" placeholder='JSON 对象，如 {"library": "x"}' />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" :disabled="!createForm.kind" @click="create">提交</el-button>
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
    { label: '标识', value: String(detail.value.id) },
    { label: '类型', value: detail.value.kind },
    { label: '状态', value: detail.value.status },
    { label: '结束时间', value: detail.value.finished_at || '' },
    { label: '错误', value: detail.value.error || '' },
  ];
});

const detailText = computed(() => {
  if (!detail.value || detail.value.result === undefined) return '（无）';
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
    error.value = err instanceof ApiError ? err.message : '加载失败';
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
    notifyError(err, '加载详情失败');
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
    await ElMessageBox.confirm('确定取消该任务吗？', '取消任务', { type: 'warning' });
  } catch {
    return;
  }
  try {
    await api<{ ok: boolean }>(`/jobs/${id}/cancel`, { method: 'POST' });
    notifySuccess('已取消');
    await loadDetail(id);
    await reload();
  } catch (err) {
    notifyError(err, '取消失败');
  }
}

async function create(): Promise<void> {
  let params: Record<string, unknown> = {};
  try {
    params = JSON.parse(createForm.paramsText || '{}') as Record<string, unknown>;
  } catch {
    notifyError(new Error('参数不是合法 JSON'), '参数错误');
    return;
  }
  try {
    const { data } = await api<{ id: number; ok: boolean }>('/jobs', {
      method: 'POST',
      body: { kind: createForm.kind, params },
    });
    notifySuccess(`任务已提交：${data.id}`);
    createOpen.value = false;
    createForm.kind = '';
    createForm.paramsText = '{}';
    await reload();
  } catch (err) {
    notifyError(err, '提交失败');
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
