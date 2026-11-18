<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.requestApprovals" :description="messages.requests.approvalDescription" />
    <LoadingPanel v-if="loading && items.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="items.length === 0" :description="messages.requests.emptyApprovals" />
      <div v-for="item in items" :key="item.id" class="libref-card libref-request-row">
        <div class="libref-review-main">
          <StatusBadge :status="item.status" />
          <span class="libref-mono">#{{ item.id }} {{ item.kind }}</span>
          <span>{{ item.target }}</span>
        </div>
        <div class="libref-muted">
          {{ item.requester || messages.common.unknown }} · {{ item.created_at }} ·
          {{ messages.requests.source }} {{ item.source || messages.common.none }}
        </div>
        <div class="libref-review-actions">
          <el-button type="success" size="small" @click="act(item, 'approve')">{{ messages.requests.approve }}</el-button>
          <el-button type="warning" size="small" @click="act(item, 'reject')">{{ messages.requests.reject }}</el-button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElMessageBox } from 'element-plus';
import { ref } from 'vue';
import { api, ApiError } from '../api/client';
import type { Envelope, IndexRequest } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import StatusBadge from '../components/common/StatusBadge.vue';
import { messages } from '../i18n';
import { notifyError, notifySuccess } from '../stores/notify';

const items = ref<IndexRequest[]>([]);
const loading = ref(false);
const error = ref('');
const requestId = ref('');

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<IndexRequest>>('/index-requests/pending', {
      params: { limit: 50 },
    });
    items.value = data.items;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

async function act(item: IndexRequest, action: 'approve' | 'reject'): Promise<void> {
  let reason = '';
  if (action === 'reject') {
    try {
      const answer = await ElMessageBox.prompt(
        messages.value.requests.reasonPrompt,
        messages.value.requests.reject,
        { inputPattern: /\S+/, inputErrorMessage: messages.value.requests.reasonRequired },
      );
      reason = answer.value;
    } catch {
      return;
    }
  }
  try {
    await api<IndexRequest>(`/index-requests/${item.id}/${action}`, {
      method: 'POST',
      body: action === 'reject' ? { reason } : undefined,
    });
    notifySuccess(messages.value.requests.processed);
    await reload();
  } catch (err) {
    notifyError(err, messages.value.requests.failed);
    if (err instanceof ApiError && err.status === 409) await reload();
  }
}

void reload();
</script>

<style scoped>
.libref-request-row { margin-bottom: var(--sp-2); }
</style>
