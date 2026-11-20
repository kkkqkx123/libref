<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.search" :description="messages.search.description" />
    <div class="libref-card">
      <el-input
        v-model="query"
         :placeholder="messages.search.placeholder"
        clearable
        @keyup.enter="search"
      >
        <template #append>
          <el-button type="primary" :loading="loading" @click="search">{{ messages.common.search }}</el-button>
        </template>
      </el-input>
      <ScopeFilter show-preset />
      <el-checkbox v-model="suggest">{{ messages.search.suggest }}</el-checkbox>
      <el-checkbox v-model="scopeStore.includePrerelease">{{ messages.common.includePrerelease }}</el-checkbox>
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
      <EmptyState v-if="(result?.items.length || 0) === 0" :description="messages.search.empty" />
      <SearchResultList v-else :items="result?.items || []" @open-read="openRead" @open-answer="openAnswer" />
      <div v-if="(result?.suggestions?.length || 0) > 0" class="libref-card">
        <h4>{{ messages.search.suggestions }}</h4>
        <RowTable :rows="result?.suggestions || []" :columns="SUGGESTION_COLUMNS" />
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
import type { ColumnKey, DiscoveryCandidate, SearchItem, SearchResponse } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import SearchResultList from '../components/business/SearchResultList.vue';
import DegradedBanner from '../components/common/DegradedBanner.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import RowTable from '../components/common/RowTable.vue';
import { scopeParams, scopeStore, type SearchPreset } from '../stores/scope';
import { applyScopeFromQuery, readBool, readNumber, readString, scopeSnapshot, useQuerySync } from '../utils/query';

// Suggestions are discovery candidates; the table shows the triage fields only.
const SUGGESTION_COLUMNS: readonly ColumnKey<DiscoveryCandidate>[] = [
  'name',
  'version',
  'ecosystem',
  'popularity',
  'docs_url',
];

const route = useRoute();
const router = useRouter();
const { sync } = useQuerySync();

applyScopeFromQuery(route.query);
const preset = readString(route.query.preset);
if (preset === 'precise' || preset === 'balanced' || preset === 'thorough') {
  scopeStore.preset = preset as SearchPreset;
}
if (route.query.limit) scopeStore.limit = Math.min(100, Math.max(1, readNumber(route.query.limit, scopeStore.limit)));
if (route.query.include_prerelease !== undefined) {
  scopeStore.includePrerelease = readBool(route.query.include_prerelease, false);
}

const query = ref(readString(route.query.query));
const suggest = ref(readBool(route.query.suggest, false));
const loading = ref(false);
const searched = ref(false);
const error = ref('');
const requestId = ref('');
const result = ref<SearchResponse | null>(null);

async function search(): Promise<void> {
  if (!query.value.trim()) return;
  sync({
    query: query.value.trim(),
    suggest: suggest.value,
    preset: scopeStore.preset,
    limit: scopeStore.limit,
    include_prerelease: scopeStore.includePrerelease,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api('/search', {
      params: {
        query: query.value.trim(),
        ...scopeParams(),
        limit: scopeStore.limit,
        preset: scopeStore.preset,
        suggest: suggest.value,
        include_prerelease: scopeStore.includePrerelease,
      },
    });
    result.value = data;
    searched.value = true;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.search.failed;
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
