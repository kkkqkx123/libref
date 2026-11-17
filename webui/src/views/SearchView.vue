<template>
  <div class="libref-page">
    <PageHeader title="搜索" description="全文加向量双路检索，自动降级并给出可回答度" />
    <div class="libref-card">
      <el-input
        v-model="query"
        placeholder="输入问题或关键词"
        clearable
        @keyup.enter="search"
      >
        <template #append>
          <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
        </template>
      </el-input>
      <ScopeFilter show-preset />
      <el-checkbox v-model="suggest">无结果时给出联想建议</el-checkbox>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="search" />
    <template v-else-if="searched">
      <DegradedBanner
        :degraded="result?.degraded || false"
        :reasons="result?.reasons || []"
        :answerable="result?.answerable"
        :answerability="result?.answerability"
      />
      <EmptyState v-if="(result?.items.length || 0) === 0" description="没有搜到结果，换个问法或放宽库版本限定" />
      <SearchResultList v-else :items="result?.items || []" @open-read="openRead" @open-answer="openAnswer" />
      <div v-if="(result?.suggestions?.length || 0) > 0" class="libref-card">
        <h4>联想建议</h4>
        <RowTable :rows="result?.suggestions || []" />
        <p v-if="result?.suggest_error" class="libref-muted">{{ result.suggest_error }}</p>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCheckbox, ElInput } from 'element-plus';
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { SearchItem, SearchResponse } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import SearchResultList from '../components/business/SearchResultList.vue';
import DegradedBanner from '../components/common/DegradedBanner.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import RowTable from '../components/common/RowTable.vue';
import { scopeParams, scopeStore } from '../stores/scope';

const route = useRoute();
const router = useRouter();
const query = ref(typeof route.query.query === 'string' ? route.query.query : '');
const suggest = ref(false);
const loading = ref(false);
const searched = ref(false);
const error = ref('');
const requestId = ref('');
const result = ref<SearchResponse | null>(null);

async function search(): Promise<void> {
  if (!query.value.trim()) return;
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<SearchResponse>('/search', {
      params: {
        query: query.value.trim(),
        ...scopeParams(),
        limit: scopeStore.limit,
        preset: scopeStore.preset,
        suggest: suggest.value,
      },
    });
    result.value = data;
    searched.value = true;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '搜索失败';
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function openRead(item: SearchItem): void {
  void router.push({
    path: '/reader',
    query: {
      url: item.url,
      library: item.library || undefined,
      version: item.version || undefined,
      ecosystem: scopeStore.ecosystem || undefined,
    },
  });
}

function openAnswer(item: SearchItem): void {
  void router.push({
    path: '/answer',
    query: {
      query: query.value,
      library: item.library || undefined,
      version: item.version || undefined,
    },
  });
}

if (query.value) void search();
</script>
