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
    <QuotaNotice v-if="quotaReset !== null" :reset-at="quotaReset">
      <el-button v-if="quotaRetry" size="small" type="primary" @click="retryQuota">
        {{ messages.common.retry }}
      </el-button>
    </QuotaNotice>
    <LoadingPanel v-if="loading && jobs.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="jobs.length === 0" :description="messages.jobs.empty" />
      <el-table v-else :data="jobs" stripe @row-click="openDetail" class="libref-w-full">
        <el-table-column prop="id" :label="messages.jobs.id" width="90" />
        <el-table-column prop="kind" :label="messages.jobs.kind" width="160" />
        <el-table-column prop="status" :label="messages.jobs.status" width="120">
          <template #default="scope"><StatusBadge :status="scope.row.status" /></template>
        </el-table-column>
        <el-table-column :label="messages.jobs.finishedAt" width="200">
          <template #default="scope">{{ format.dateTime(scope.row.finished_at) }}</template>
        </el-table-column>
        <el-table-column prop="error" :label="messages.jobs.errorSummary" show-overflow-tooltip />
      </el-table>
    </template>

    <el-drawer v-model="detailOpen"  :title="messages.jobs.detailTitle" size="480px" @closed="closeDetail">
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
          <el-button v-if="canResubmit" type="primary" @click="resubmit">{{ messages.jobs.resubmit }}</el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="createOpen"  :title="messages.jobs.createTitle" width="520px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item :label="messages.common.kind">
          <el-select v-model="createForm.kind">
            <el-option value="mine_patterns" :label="messages.jobs.minePatterns" />
            <el-option value="induce_labels" :label="messages.jobs.induceLabels" />
          </el-select>
        </el-form-item>
        <el-form-item :label="messages.common.library">
          <el-input v-model="createForm.library" />
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="createForm.version" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">{{ messages.common.cancel }}</el-button>
        <el-button type="primary" :disabled="!createForm.library || !createForm.version" @click="create">{{ messages.common.submit }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElDialog, ElDrawer, ElForm, ElFormItem, ElInput, ElInputNumber, ElOption, ElSelect, ElTable, ElTableColumn } from 'element-plus';
import { ElMessageBox } from 'element-plus';
import { computed, onUnmounted, reactive, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { JobCreate, JobDetail, JobSummary } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import QuotaNotice from '../components/common/QuotaNotice.vue';
import { messages } from '../i18n';
import { useFormatters } from '../i18n/format';
import StatusBadge from '../components/common/StatusBadge.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';
import { useQuerySync } from '../utils/query';

const route = useRoute();
const { sync } = useQuerySync();
const format = useFormatters();
const canCreate = computed(() => hasScope('jobs:write'));
const limit = ref(20);
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const jobs = ref<JobSummary[]>([]);
const detailOpen = ref(false);
const detailLoading = ref(false);
const detail = ref<JobDetail | null>(null);
const createOpen = ref(false);
const createForm = reactive({
  kind: 'mine_patterns' as 'mine_patterns' | 'induce_labels',
  library: '',
  version: '',
});
// 429 is a quota refusal, not a failure: show the reset time plus a manual retry
// instead of an error notification.
const quotaReset = ref<string | null>(null);
const quotaRetry = ref<null | (() => void)>(null);

function handleWriteFailure(err: unknown, title: string, retry?: () => void): void {
  if (err instanceof ApiError && err.status === 429) {
    quotaReset.value = err.resetAt || '';
    quotaRetry.value = retry || null;
    return;
  }
  notifyError(err, title);
}

function retryQuota(): void {
  const retry = quotaRetry.value;
  quotaReset.value = null;
  quotaRetry.value = null;
  if (retry) retry();
}
let timer: number | null = null;
let listTimer: number | null = null;

const hasActiveJobs = computed(() =>
  jobs.value.some((job) => job.status === 'pending' || job.status === 'running'),
);

const paramsText = computed(() => {
  const params = detail.value?.params;
  if (!params || Object.keys(params).length === 0) return messages.value.jobs.none;
  try {
    return JSON.stringify(params);
  } catch {
    return String(params);
  }
});

const detailEntries = computed<KeyValueEntry[]>(() => {
  if (!detail.value) return [];
  return [
    { label: messages.value.jobs.id, value: String(detail.value.id) },
    { label: messages.value.jobs.kind, value: detail.value.kind },
    { label: messages.value.jobs.status, value: detail.value.status },
    { label: messages.value.jobs.finishedAt, value: format.dateTime(detail.value.finished_at) },
    { label: messages.value.jobs.error, value: detail.value.error || '' },
    { label: messages.value.jobs.params, value: paramsText.value },
  ];
});


// Only a failed job with recorded parameters can be replayed; replaying a
// successful one would duplicate work the operator did not ask for.
const canResubmit = computed(() => {
  const current = detail.value;
  if (!current || current.status !== 'failed' || !canCreate.value) return false;
  return Object.keys(current.params || {}).length > 0;
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
    const { data } = await api('/jobs', { params: { limit: limit.value } });
    jobs.value = data.jobs;
    ensureListPolling();
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
    const { data } = await api('/jobs/{job_id}', { pathParams: { job_id: id } });
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
  sync({ id: row.id });
  void loadDetail(row.id);
}

function closeDetail(): void {
  stopPolling();
  sync({ id: undefined });
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

// Queued and running jobs move on their own; the list follows until every row
// reaches a terminal state, then polling stops by itself.
function ensureListPolling(): void {
  if (hasActiveJobs.value && listTimer === null) {
    listTimer = window.setInterval(() => {
      void reload();
    }, 5000);
    return;
  }
  if (!hasActiveJobs.value) stopListPolling();
}

function stopListPolling(): void {
  if (listTimer !== null) {
    window.clearInterval(listTimer);
    listTimer = null;
  }
}

async function resubmit(): Promise<void> {
  const current = detail.value;
  if (!current) return;
  if (current.kind !== 'mine_patterns' && current.kind !== 'induce_labels') {
    notifyError(new Error(messages.value.jobs.indexJobHint), messages.value.common.paramError);
    return;
  }
  const params = {
    library: current.params.library,
    version: current.params.version,
    include_ambiguous: false,
  };
  const body: JobCreate =
    current.kind === 'mine_patterns'
      ? { kind: 'mine_patterns', params }
      : { kind: 'induce_labels', params };
  try {
    const { data } = await api('/jobs', { method: 'POST', body });
    notifySuccess(messages.value.jobs.submitted(data.id));
    detailOpen.value = false;
    await reload();
  } catch (err) {
    handleWriteFailure(err, messages.value.jobs.resubmitFailed, () => void resubmit());
  }
}

async function cancel(id: number): Promise<void> {
  try {
    await ElMessageBox.confirm(messages.value.jobs.cancelConfirm, messages.value.jobs.cancelTask, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await api('/jobs/{job_id}/cancel', { method: 'POST', pathParams: { job_id: id } });
    notifySuccess(messages.value.jobs.cancelled);
    await loadDetail(id);
    await reload();
  } catch (err) {
    handleWriteFailure(err, messages.value.jobs.cancelFailed, () => void cancel(id));
  }
}

async function create(): Promise<void> {
  const params = {
    library: createForm.library.trim(),
    version: createForm.version.trim(),
    include_ambiguous: false,
  };
  if (!params.library || !params.version) {
    notifyError(new Error(messages.value.common.paramError), messages.value.common.paramError);
    return;
  }
  const body: JobCreate =
    createForm.kind === 'mine_patterns'
      ? { kind: 'mine_patterns', params }
      : { kind: 'induce_labels', params };
  try {
    const { data } = await api('/jobs', { method: 'POST', body });
    notifySuccess(messages.value.jobs.submitted(data.id));
    createOpen.value = false;
    createForm.library = '';
    createForm.version = '';
    await reload();
  } catch (err) {
    handleWriteFailure(err, messages.value.jobs.submitFailed, () => void create());
  }
}

watch(hasActiveJobs, (active) => {
  if (active) ensureListPolling();
  else stopListPolling();
});

onUnmounted(() => {
  stopPolling();
  stopListPolling();
});

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
  padding: var(--sp-3);
}
.libref-detail-actions {
  margin-top: var(--sp-4);
}
</style>
