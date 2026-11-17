<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.patterns" :description="messages.patternInsight.description" />
    <div class="libref-card">
      <h4>{{ messages.patternInsight.miningHealth }}</h4>
      <LoadingPanel v-if="healthLoading" :rows="2" />
      <div v-else-if="health" class="libref-health-grid">
        <KeyValue :entries="healthEntries" />
      </div>
      <p v-else class="libref-muted">{{ messages.patternInsight.healthUnavailable }}</p>
    </div>
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item :label="messages.common.view">
          <el-radio-group v-model="view">
            <el-radio-button label="patterns">{{ messages.patternInsight.patterns }}</el-radio-button>
            <el-radio-button label="evidence">{{ messages.patternInsight.evidence }}</el-radio-button>
            <el-radio-button label="tree">{{ messages.patternInsight.tree }}</el-radio-button>
            <el-radio-button label="clusters">{{ messages.patternInsight.clusters }}</el-radio-button>
            <el-radio-button label="health">{{ messages.patternInsight.health }}</el-radio-button>
            <el-radio-button label="labels">{{ messages.common.labels }}</el-radio-button>
            <el-radio-button label="snippets">{{ messages.patternInsight.snippets }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="messages.patternInsight.pattern">
          <TagInput v-model="pattern"  :placeholder="messages.patternInsight.patternPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.common.containing">
          <TagInput v-model="containing"  :placeholder="messages.patternInsight.containingPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.patternInsight.prefix">
          <el-input v-model="prefix" clearable style="width: 220px" />
        </el-form-item>
        <el-form-item :label="messages.common.dedup">
          <el-checkbox v-model="dedup">{{ messages.patternInsight.dedupByFile }}</el-checkbox>
        </el-form-item>
        <el-form-item :label="messages.patternInsight.perFileLimit">
          <el-input-number v-model="perFileLimit" :min="1" :max="20" />
        </el-form-item>
        <ScopeFilter />
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="load">{{ messages.common.query }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <template v-else-if="loaded">
      <EmptyState v-if="rows.length === 0" :description="messages.patternInsight.empty" />
      <template v-else>
        <EvidenceList v-if="view === 'evidence'" :rows="rows" @feedback="forwardFeedback" />
        <RowTable v-else :rows="rows" />
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCheckbox, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup } from 'element-plus';
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EvidenceList from '../components/business/EvidenceList.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import RowTable from '../components/common/RowTable.vue';
import TagInput from '../components/common/TagInput.vue';
import { scopeParams } from '../stores/scope';
import {
  applyScopeFromQuery,
  readBool,
  readList,
  readNumber,
  readString,
  scopeSnapshot,
  useQuerySync,
} from '../utils/query';

const route = useRoute();
const { sync } = useQuerySync();

applyScopeFromQuery(route.query);

const router = useRouter();
type View = 'patterns' | 'evidence' | 'tree' | 'clusters' | 'health' | 'labels' | 'snippets';
const VIEWS: View[] = ['patterns', 'evidence', 'tree', 'clusters', 'health', 'labels', 'snippets'];
const requested = readString(route.query.view) as View;
const view = ref<View>(VIEWS.includes(requested) ? requested : 'patterns');
const pattern = ref<string[]>(readList(route.query.pattern));
const containing = ref<string[]>(readList(route.query.containing));
const prefix = ref(readString(route.query.prefix));
const dedup = ref(readBool(route.query.dedup, false));
const perFileLimit = ref<number | undefined>(
  route.query.per_file_limit ? readNumber(route.query.per_file_limit, 1) : undefined,
);
const limit = ref(readNumber(route.query.limit, 50));
const loading = ref(false);
const loaded = ref(false);
const error = ref('');
const requestId = ref('');
const rows = ref<Row[]>([]);
const health = ref<Row | null>(null);
const healthLoading = ref(false);

const healthEntries = computed<KeyValueEntry[]>(() => {
  if (!health.value) return [];
  return Object.entries(health.value).map(([label, value]) => ({
    label,
    value: typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''),
  }));
});

async function loadHealth(): Promise<void> {
  healthLoading.value = true;
  try {
    const { data } = await api<Row>('/mining-health', { params: { ...scopeParams() } });
    health.value = data;
  } catch {
    health.value = null;
  } finally {
    healthLoading.value = false;
  }
}

async function load(): Promise<void> {
  sync({
    view: view.value,
    pattern: pattern.value,
    containing: containing.value,
    prefix: prefix.value,
    dedup: dedup.value,
    per_file_limit: perFileLimit.value,
    limit: limit.value,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<Row>>('/pattern-insight', {
      params: {
        view: view.value,
        pattern: pattern.value.length > 0 ? pattern.value.join(',') : undefined,
        containing: containing.value.length > 0 ? containing.value.join(',') : undefined,
        prefix: prefix.value || undefined,
        ...scopeParams(),
        limit: limit.value,
        per_file_limit: perFileLimit.value,
        dedup: dedup.value,
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

function forwardFeedback(row: Row): void {
  const entityId = Number(row['entity_id']);
  const pattern = String(row['pattern'] || '');
  void router.push({
    path: '/feedback',
    query: {
      entity_type: 'call_instance',
      entity_id: Number.isFinite(entityId) && entityId > 0 ? String(entityId) : undefined,
      body: pattern,
    },
  });
}

void loadHealth();
</script>

<style scoped>
.libref-health-grid {
  max-width: 640px;
}
</style>
