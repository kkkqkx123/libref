<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.docInsight" :description="messages.docInsight.description" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item :label="messages.common.view">
          <el-radio-group v-model="view">
            <el-radio-button v-for="name in VIEWS" :key="name" :value="name">
              {{ messages.docInsight[name] }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="messages.docInsight.facet">
          <el-input v-model="facet" clearable class="libref-w-220" />
        </el-form-item>
        <el-form-item :label="messages.docInsight.api">
          <el-input v-model="apiFilter" clearable class="libref-w-220" />
        </el-form-item>
        <el-form-item :label="messages.patternInsight.pattern">
          <el-input v-model="pattern" clearable class="libref-w-220" />
        </el-form-item>
        <el-form-item :label="messages.docInsight.verdict">
          <el-select v-model="verdict" clearable class="libref-w-160">
            <el-option v-for="item in VERDICTS" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item :label="messages.docInsight.blockType">
          <el-select v-model="blockType" clearable class="libref-w-160">
            <el-option v-for="item in BLOCK_TYPES" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item :label="messages.docInsight.documentId">
          <el-input-number v-model="documentId" :min="1" />
        </el-form-item>
        <ScopeFilter show-limit />
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="load">{{ messages.common.query }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <template v-else-if="loaded">
      <EmptyState v-if="rows.length === 0" :description="messages.docInsight.empty" />
      <RowTable v-else :rows="rows" :columns="columns" />
    </template>
  </div>
</template>

<script setup lang="ts">
import {
  ElButton,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
} from 'element-plus';
import { computed, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import { type ColumnKey, type DocInsightRow } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import RowTable from '../components/common/RowTable.vue';
import { messages } from '../i18n';
import { scopeStore } from '../stores/scope';
import {
  applyScopeFromQuery,
  readNumber,
  readString,
  scopeSnapshot,
  useQuerySync,
} from '../utils/query';

const VIEWS = ['features', 'links', 'coverage', 'consistency'] as const;
type View = (typeof VIEWS)[number];
const VERDICTS = ['match', 'drift', 'unknown'] as const;
const BLOCK_TYPES = ['narrative', 'example', 'params', 'warning', 'changelog'] as const;

const COLUMNS: Record<View, readonly ColumnKey<DocInsightRow>[]> = {
  features: ['document_id', 'facet', 'kind', 'details'],
  links: ['document_id', 'api', 'match_kind', 'confidence', 'feature_nodes'],
  coverage: ['document_id', 'pattern', 'covered_kind', 'support'],
  consistency: ['document_id', 'facet', 'verdict', 'note', 'model'],
};

const route = useRoute();
const { sync } = useQuerySync();
applyScopeFromQuery(route.query);

const requested = readString(route.query.view) as View;
const view = ref<View>(VIEWS.includes(requested) ? requested : 'features');
const facet = ref(readString(route.query.facet));
const apiFilter = ref(readString(route.query.api));
const pattern = ref(readString(route.query.pattern));
const verdict = ref(readString(route.query.verdict));
const blockType = ref(readString(route.query.block_type));
const documentId = ref<number | undefined>(
  route.query.document_id ? readNumber(route.query.document_id, 1) : undefined,
);
const columns = computed(() => COLUMNS[view.value]);
const loading = ref(false);
const loaded = ref(false);
const error = ref('');
const requestId = ref('');
const rows = ref<DocInsightRow[]>([]);

async function load(): Promise<void> {
  sync({
    view: view.value,
    facet: facet.value || undefined,
    api: apiFilter.value || undefined,
    pattern: pattern.value || undefined,
    verdict: verdict.value || undefined,
    block_type: blockType.value || undefined,
    document_id: documentId.value,
    limit: scopeStore.resultLimit,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  if (!scopeStore.library || !scopeStore.version) {
    rows.value = [];
    loaded.value = true;
    loading.value = false;
    return;
  }
  try {
    const { data } = await api('/doc-insight', {
      params: {
        view: view.value,
        library: scopeStore.library,
        version: scopeStore.version,
        ecosystem: scopeStore.ecosystem || undefined,
        facet: facet.value || undefined,
        api: apiFilter.value || undefined,
        pattern: pattern.value || undefined,
        verdict: verdict.value || undefined,
        block_type: blockType.value || undefined,
        document_id: documentId.value,
        limit: scopeStore.resultLimit,
      },
    });
    rows.value = data.items;
    loaded.value = true;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.queryFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}
</script>
