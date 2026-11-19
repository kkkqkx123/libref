<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.answer" :description="messages.answer.description" />
    <div class="libref-card">
      <el-input
        v-model="query"
         :placeholder="messages.answer.placeholder"
        clearable
        type="textarea"
        :rows="2"
        @keyup.enter="ask"
      />
      <ScopeFilter show-preset />
      <div class="libref-answer-limit">
        <span class="libref-muted">{{ messages.common.limit }}</span>
        <el-input-number v-model="answerLimit" :min="1" :max="50" class="libref-w-120"/>
      </div>
      <el-button type="primary" :loading="loading" :disabled="!query.trim()" @click="ask">
        {{ messages.common.ask }}
      </el-button>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="ask" />
    <QuotaNotice v-else-if="quotaReset !== null" :reset-at="quotaReset">
      <el-button size="small" type="primary" @click="viewSearch">{{ messages.answer.viewSearch }}</el-button>
    </QuotaNotice>
    <template v-else-if="answer">
      <DegradedBanner
        :degraded="answer.degraded"
        :reasons="answer.reasons"
        :answerable="answer.answer !== ''"
      />
      <EmptyState v-if="!answer.answer" :description="messages.answer.empty" />
      <div v-else class="libref-card">
        <div class="libref-answer-meta">
          <el-tag size="small" :type="answer.confidence === 'high' ? 'success' : 'warning'">
            {{ messages.answer.confidence }} {{ answer.confidence }}
          </el-tag>
          <span class="libref-muted">{{ messages.answer.model }} {{ answer.model || messages.common.unknown }}</span>
        </div>
        <p class="libref-answer-body">{{ answer.answer }}</p>
      </div>
      <div v-if="answer.citations.length > 0" class="libref-card">
        <h4>{{ messages.answer.citationsCount(answer.citations.length) }}</h4>
        <CitationCard v-for="citation in answer.citations" :key="citation.id" :citation="citation" />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElInput, ElInputNumber, ElTag } from 'element-plus';
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { AnswerResponse } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import CitationCard from '../components/common/CitationCard.vue';
import DegradedBanner from '../components/common/DegradedBanner.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import QuotaNotice from '../components/common/QuotaNotice.vue';
import { messages } from '../i18n';
import { scopeParams, scopeStore } from '../stores/scope';
import { applyScopeFromQuery, readNumber, readString, scopeSnapshot, useQuerySync } from '../utils/query';

const route = useRoute();
const router = useRouter();
const { sync } = useQuerySync();

applyScopeFromQuery(route.query);
const query = ref(readString(route.query.query));
const answerLimit = ref(Math.min(50, Math.max(1, readNumber(route.query.answer_limit, 5))));
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const quotaReset = ref<string | null>(null);
const answer = ref<AnswerResponse | null>(null);

async function ask(): Promise<void> {
  if (!query.value.trim()) return;
  sync({
    query: query.value.trim(),
    preset: scopeStore.preset,
    answer_limit: answerLimit.value,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  quotaReset.value = null;
  answer.value = null;
  try {
    const { data } = await api('/answer', {
      params: {
        query: query.value.trim(),
        ...scopeParams(),
        limit: answerLimit.value,
        preset: scopeStore.preset,
      },
    });
    answer.value = data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 429) {
      quotaReset.value = err.resetAt || '';
      return;
    }
    error.value = err instanceof ApiError ? err.message : messages.value.answer.failed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function viewSearch(): void {
  void router.push({
    path: '/search',
    query: { query: query.value.trim(), ...scopeSnapshot() as Record<string, string> },
  });
}

if (query.value) void ask();
</script>

<style scoped>
.libref-answer-meta {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  margin-bottom: var(--sp-2);
}
.libref-answer-body {
  white-space: pre-wrap;
  line-height: 1.7;
}
.libref-answer-limit {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  margin-right: var(--sp-3);
}
</style>
