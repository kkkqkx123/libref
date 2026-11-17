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
      <el-button type="primary" :loading="loading" :disabled="!query.trim()" @click="ask">
        {{ messages.common.ask }}
      </el-button>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="ask" />
    <template v-else-if="answer">
      <DegradedBanner
        :degraded="answer.degraded"
        :reasons="answer.reasons"
        :answerable="answer.answer !== ''"
        :answerability="answer.answer === '' ? 0 : 1"
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
        <h4>{{ messages.answer.citations }}（{{ answer.citations.length }}）</h4>
        <CitationCard v-for="citation in answer.citations" :key="citation.id" :citation="citation" />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElInput, ElTag } from 'element-plus';
import { ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { AnswerResponse } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import CitationCard from '../components/common/CitationCard.vue';
import DegradedBanner from '../components/common/DegradedBanner.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import { scopeParams, scopeStore } from '../stores/scope';

const route = useRoute();
const query = ref(typeof route.query.query === 'string' ? route.query.query : '');
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const answer = ref<AnswerResponse | null>(null);

if (typeof route.query.library === 'string' && route.query.library) {
  scopeStore.library = route.query.library;
}
if (typeof route.query.version === 'string' && route.query.version) {
  scopeStore.version = route.query.version;
}

async function ask(): Promise<void> {
  if (!query.value.trim()) return;
  loading.value = true;
  error.value = '';
  answer.value = null;
  try {
    const { data } = await api<AnswerResponse>('/answer', {
      params: {
        query: query.value.trim(),
        ...scopeParams(),
        limit: 5,
        preset: scopeStore.preset,
      },
    });
    answer.value = data;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.answer.failed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

if (query.value) void ask();
</script>

<style scoped>
.libref-answer-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
.libref-answer-body {
  white-space: pre-wrap;
  line-height: 1.7;
}
</style>
