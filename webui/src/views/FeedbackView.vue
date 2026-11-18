<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.feedback" :description="messages.feedback.description" />
    <QuotaNotice v-if="quotaReset !== null" :reset-at="quotaReset">
      <el-button size="small" type="primary" :loading="submitting" @click="retryQuota">
        {{ messages.common.retry }}
      </el-button>
    </QuotaNotice>
    <div v-if="canSubmit" class="libref-card">
      <h4>{{ messages.feedback.submitTitle }}</h4>
      <el-form :model="form" label-width="90px" @submit.prevent="submit">
        <el-form-item :label="messages.feedback.entityType">
          <el-input v-model="form.entity_type" class="libref-w-220"/>
        </el-form-item>
        <el-form-item :label="messages.feedback.entityId">
          <el-input-number v-model="form.entity_id" :min="0" />
        </el-form-item>
        <el-form-item :label="messages.common.kind">
          <el-input v-model="form.kind" class="libref-w-220"/>
        </el-form-item>
        <el-form-item :label="messages.feedback.body">
          <el-input v-model="form.body" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" :disabled="!form.body.trim()" @click="submit">
            {{ messages.common.submit }}
          </el-button>
        </el-form-item>
      </el-form>
    </div>
    <p v-if="ownOnly" class="libref-muted">{{ messages.feedback.ownOnlyHint }}</p>
    <div v-if="canRead" class="libref-card">
      <el-form :inline="true" @submit.prevent="reload">
        <el-form-item :label="messages.common.status">
          <el-select v-model="status" clearable  :placeholder="messages.common.all" @change="reload" class="libref-w-160">
            <el-option :label="messages.feedback.open" value="open" />
            <el-option :label="messages.feedback.acknowledged" value="acknowledged" />
            <el-option :label="messages.feedback.resolved" value="resolved" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button @click="reload">{{ messages.common.refresh }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="canRead && loading && items.length === 0" />
    <ErrorState v-else-if="canRead && error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else-if="canRead">
      <EmptyState v-if="items.length === 0" :description="messages.feedback.empty" />
      <div
        v-for="item in items"
        :key="item.id"
        class="libref-card"
        :class="{ 'libref-feedback-resolved': item.status === 'resolved' }"
      >
        <div class="libref-feedback-head">
          <StatusBadge :status="item.status" />
          <span class="libref-mono">{{ item.entity_type }}#{{ item.entity_id }}</span>
          <span class="libref-muted">{{ item.kind }}</span>
          <span class="libref-muted">{{ format.dateTime(item.created_at) }}</span>
          <el-button
            v-if="canResolve && item.status !== 'resolved'"
            link
            type="primary"
            size="small"
            @click="resolve(item)"
          >
            {{ messages.feedback.resolve }}
          </el-button>
        </div>
        <p>{{ item.body }}</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElInputNumber, ElOption, ElSelect } from 'element-plus';
import { computed, reactive, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, FeedbackItem } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import QuotaNotice from '../components/common/QuotaNotice.vue';
import { messages } from '../i18n';
import { useFormatters } from '../i18n/format';
import StatusBadge from '../components/common/StatusBadge.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';
import { readNumber, readString, useQuerySync } from '../utils/query';

const route = useRoute();
const { sync } = useQuerySync();
const format = useFormatters();
const canSubmit = computed(() => hasScope('feedback:write'));
const canReadAll = computed(() => hasScope('feedback:read'));
const canReadOwn = computed(() => hasScope('feedback:read:own'));
// Reading is split: handlers see the tenant list, authors see only their own rows.
const canRead = computed(() => canReadAll.value || canReadOwn.value);
const ownOnly = computed(() => !canReadAll.value && canReadOwn.value);
const canResolve = computed(() => hasScope('feedback:resolve'));

const form = reactive({
  entity_type: typeof route.query.entity_type === 'string' ? route.query.entity_type : 'call_instance',
  entity_id: route.query.entity_id ? readNumber(route.query.entity_id, 0) : 0,
  kind: 'general',
  body: typeof route.query.body === 'string' ? route.query.body : '',
});
const status = ref(readString(route.query.status));
const loading = ref(false);
const submitting = ref(false);
// Quota refusals show the reset time plus a manual retry rather than an error.
const quotaReset = ref<string | null>(null);
const quotaRetry = ref<null | (() => void)>(null);

function retryQuota(): void {
  const retry = quotaRetry.value;
  quotaReset.value = null;
  quotaRetry.value = null;
  if (retry) retry();
  else void submit();
}
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
    notifySuccess(messages.value.feedback.submitted);
    form.body = '';
    if (canRead.value) await reload();
  } catch (err) {
    if (err instanceof ApiError && err.status === 429) {
      quotaReset.value = err.resetAt || '';
    } else {
      notifyError(err, messages.value.common.submitFailed);
    }
  } finally {
    submitting.value = false;
  }
}

async function reload(): Promise<void> {
  sync({ status: status.value });
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<FeedbackItem>>('/feedback', {
      params: { limit: 50, status: status.value || undefined },
    });
    items.value = data.items;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

async function resolve(item: FeedbackItem): Promise<void> {
  try {
    await api<{ ok: boolean }>(`/feedback/${item.id}/resolve`, { method: 'POST' });
    notifySuccess(messages.value.feedback.resolvedDone);
    item.status = 'resolved';
  } catch (err) {
    if (err instanceof ApiError && err.status === 429) {
      quotaReset.value = err.resetAt || '';
      quotaRetry.value = () => void resolve(item);
      return;
    }
    notifyError(err, messages.value.feedback.actionFailed);
  }
}

if (canRead.value) void reload();
</script>

<style scoped>
.libref-feedback-resolved {
  opacity: 0.6;
}
.libref-feedback-head {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
}
</style>
