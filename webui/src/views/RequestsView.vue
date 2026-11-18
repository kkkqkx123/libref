<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.requests" :description="messages.requests.description" />
    <div class="libref-card">
      <el-form :model="form" label-width="130px" @submit.prevent="submit">
        <el-form-item :label="messages.common.kind">
          <el-radio-group v-model="form.kind">
            <el-radio-button value="repo">{{ messages.common.repo }}</el-radio-button>
            <el-radio-button value="docs">{{ messages.common.docs }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="messages.common.library">
          <el-input v-model="form.library" :placeholder="messages.requests.libraryPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="form.version" :placeholder="messages.requests.versionPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.common.ecosystem">
          <el-input v-model="form.ecosystem" />
        </el-form-item>
        <el-form-item v-if="form.kind === 'repo'" :label="messages.requests.source">
          <el-input v-model="form.source" :placeholder="messages.requests.sourcePlaceholder" />
        </el-form-item>
        <el-form-item v-else :label="messages.requests.startUrl">
          <el-input v-model="form.startUrl" :placeholder="messages.requests.startUrlPlaceholder" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submit">{{ messages.common.submit }}</el-button>
          <el-button :loading="probing" @click="probe">{{ messages.common.probe }}</el-button>
        </el-form-item>
      </el-form>
      <div v-if="probeResult" class="libref-muted">{{ probeResult }}</div>
    </div>

    <LoadingPanel v-if="loading && items.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="items.length === 0" :description="messages.requests.empty" />
      <div v-for="item in items" :key="item.id" class="libref-card libref-request-row">
        <div class="libref-review-main">
          <StatusBadge :status="item.status" />
          <span class="libref-mono">#{{ item.id }} {{ item.kind }}</span>
          <span>{{ item.target }}</span>
        </div>
        <div class="libref-muted">
          {{ item.requester || messages.common.unknown }} · {{ item.created_at }} ·
          {{ messages.requests.job }} {{ item.job_id ?? messages.common.none }}
        </div>
        <div v-if="item.reject_reason || item.error" class="libref-muted">
          {{ item.reject_reason || item.error }}
        </div>
        <div class="libref-review-actions">
          <el-button size="small" :disabled="!['pending', 'approved', 'queued'].includes(item.status)" @click="cancel(item)">
            {{ messages.common.cancel }}
          </el-button>
          <el-button size="small" @click="reapply(item)">{{ messages.requests.reapply }}</el-button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElRadioButton, ElRadioGroup } from 'element-plus';
import { ref } from 'vue';
import { api, ApiError } from '../api/client';
import type { Envelope, IndexRequest, IndexRequestCreate } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import StatusBadge from '../components/common/StatusBadge.vue';
import { messages } from '../i18n';
import { notifyError, notifySuccess } from '../stores/notify';

const form = ref({ kind: 'repo' as 'repo' | 'docs', library: '', version: '', ecosystem: '', source: '', startUrl: '' });
const items = ref<IndexRequest[]>([]);
const loading = ref(false);
const submitting = ref(false);
const probing = ref(false);
const error = ref('');
const requestId = ref('');
const probeResult = ref('');

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<IndexRequest>>('/index-requests', { params: { limit: 50 } });
    items.value = data.items;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function payload(): IndexRequestCreate {
  const base: IndexRequestCreate = {
    kind: form.value.kind,
    library: form.value.library,
    version: form.value.version,
  };
  if (form.value.ecosystem) base.ecosystem = form.value.ecosystem;
  if (form.value.kind === 'repo') base.source = form.value.source;
  else base.start_url = form.value.startUrl;
  return base;
}

async function submit(): Promise<void> {
  submitting.value = true;
  try {
    const { data } = await api<IndexRequest>('/index-requests', { method: 'POST', body: payload() });
    notifySuccess(messages.value.requests.submitted(String(data.id)));
    await reload();
  } catch (err) {
    notifyError(err, messages.value.requests.submitFailed);
  } finally {
    submitting.value = false;
  }
}

async function probe(): Promise<void> {
  probing.value = true;
  probeResult.value = '';
  try {
    if (form.value.kind === 'repo') {
      const { data } = await api<{ root_kind: string; effective_files: number }>('/repo-probe', {
        method: 'POST',
        body: { source: form.value.source },
      });
      probeResult.value = messages.value.requests.probeResult(data.root_kind, data.effective_files);
    } else {
      const { data } = await api<{ total_pages?: number; eligible?: number }>('/doc-probe', {
        method: 'POST',
        body: { start_url: form.value.startUrl },
      });
      probeResult.value = messages.value.requests.probeResult(String(data.total_pages ?? data.eligible ?? 0), '');
    }
  } catch (err) {
    notifyError(err, messages.value.requests.probeFailed);
  } finally {
    probing.value = false;
  }
}

async function cancel(item: IndexRequest): Promise<void> {
  try {
    await api<IndexRequest>(`/index-requests/${item.id}/cancel`, { method: 'POST' });
    await reload();
  } catch (err) {
    notifyError(err, messages.value.requests.cancelFailed);
  }
}

function reapply(item: IndexRequest): void {
  form.value.kind = item.kind;
  form.value.library = String(item.params.library || '');
  form.value.version = String(item.params.version || '');
  form.value.ecosystem = String(item.params.ecosystem || '');
  form.value.source = item.kind === 'repo' ? String(item.params.source || '') : '';
  form.value.startUrl = item.kind === 'docs' ? String(item.params.start_url || '') : '';
}

void reload();
</script>

<style scoped>
.libref-request-row { margin-bottom: 8px; }
</style>
