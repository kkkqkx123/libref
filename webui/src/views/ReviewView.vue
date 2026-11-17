<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.review" :description="messages.review.description" />
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent="reload">
        <el-form-item :label="messages.common.limit">
          <el-input-number v-model="limit" :min="1" :max="500" @change="reload" />
        </el-form-item>
        <el-form-item>
          <el-button @click="reload">{{ messages.common.refresh }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading && items.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="items.length === 0" :description="messages.review.empty" />
      <div v-for="item in items" :key="item.entity_id" class="libref-card libref-review-row">
        <div class="libref-review-main">
          <ConfidenceBadge :confidence="String(item.confidence || '')" />
          <span class="libref-mono">{{ item.callee }}</span>
          <span class="libref-muted">{{ item.caller_file }}:{{ item.line_start }}-{{ item.line_end }}</span>
        </div>
        <div class="libref-muted">{{ messages.review.caller }} {{ item.caller || messages.common.unknown }} · {{ messages.review.args }} {{ formatArgs(item.args) }}</div>
        <div class="libref-review-actions">
          <el-button type="success" size="small" :disabled="!canWrite" @click="act(item, 'approve')">{{ messages.review.approve }}</el-button>
          <el-button type="warning" size="small" :disabled="!canWrite" @click="act(item, 'conflict')">{{ messages.review.conflict }}</el-button>
          <el-button size="small" :disabled="!canWrite" @click="act(item, 'reopen')">{{ messages.review.reopen }}</el-button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInputNumber } from 'element-plus';
import { ref } from 'vue';
import { api, ApiError } from '../api/client';
import type { Envelope, ReviewItem } from '../api/types';
import ConfidenceBadge from '../components/common/ConfidenceBadge.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';

const canWrite = hasScope('review:write');
const limit = ref(50);
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const items = ref<ReviewItem[]>([]);

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<ReviewItem>>('/review', { params: { limit: limit.value } });
    items.value = data.items;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function formatArgs(args: unknown): string {
  if (args === null || args === undefined) return messages.value.common.none;
  if (Array.isArray(args)) return args.map((item) => String(item)).join(', ') || messages.value.common.none;
  return String(args);
}

async function act(item: ReviewItem, action: 'approve' | 'conflict' | 'reopen'): Promise<void> {
  try {
    await api<{ ok: boolean }>('/review', {
      method: 'POST',
      body: { action, entity_id: item.entity_id, limit: limit.value },
    });
    notifySuccess(messages.value.review.processed);
    items.value = items.value.filter((entry) => entry.entity_id !== item.entity_id);
    if (items.value.length === 0) await reload();
  } catch (err) {
    notifyError(err, messages.value.review.failed);
  }
}

void reload();
</script>

<style scoped>
.libref-review-main {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 4px;
}
.libref-review-actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}
</style>
