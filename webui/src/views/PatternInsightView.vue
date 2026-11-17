<template>
  <div class="libref-page">
    <PageHeader title="模式" description="序列挖掘的模式、证据、聚类与健康度" />
    <div class="libref-card">
      <h4>挖掘健康度</h4>
      <LoadingPanel v-if="healthLoading" :rows="2" />
      <div v-else-if="health" class="libref-health-grid">
        <KeyValue :entries="healthEntries" />
      </div>
      <p v-else class="libref-muted">健康度暂不可用</p>
    </div>
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item label="视图">
          <el-radio-group v-model="view">
            <el-radio-button label="patterns">模式</el-radio-button>
            <el-radio-button label="evidence">证据</el-radio-button>
            <el-radio-button label="tree">特征树</el-radio-button>
            <el-radio-button label="clusters">聚类</el-radio-button>
            <el-radio-button label="health">健康</el-radio-button>
            <el-radio-button label="labels">标签</el-radio-button>
            <el-radio-button label="snippets">片段</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="序列">
          <TagInput v-model="pattern" placeholder="模式序列，逗号分隔" />
        </el-form-item>
        <el-form-item label="包含">
          <TagInput v-model="containing" placeholder="包含的调用，逗号分隔" />
        </el-form-item>
        <el-form-item label="前缀">
          <el-input v-model="prefix" clearable style="width: 220px" />
        </el-form-item>
        <el-form-item label="去重">
          <el-checkbox v-model="dedup">按文件去重</el-checkbox>
        </el-form-item>
        <el-form-item label="单文件上限">
          <el-input-number v-model="perFileLimit" :min="1" :max="20" />
        </el-form-item>
        <ScopeFilter />
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="load">查询</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <template v-else-if="loaded">
      <EmptyState v-if="rows.length === 0" description="该条件下暂无模式" />
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
import { useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EvidenceList from '../components/business/EvidenceList.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import RowTable from '../components/common/RowTable.vue';
import TagInput from '../components/common/TagInput.vue';
import { scopeParams } from '../stores/scope';

const router = useRouter();
type View = 'patterns' | 'evidence' | 'tree' | 'clusters' | 'health' | 'labels' | 'snippets';
const view = ref<View>('patterns');
const pattern = ref<string[]>([]);
const containing = ref<string[]>([]);
const prefix = ref('');
const dedup = ref(false);
const perFileLimit = ref<number | undefined>(undefined);
const limit = ref(50);
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
    error.value = err instanceof ApiError ? err.message : '查询失败';
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function forwardFeedback(row: Row): void {
  void router.push({
    path: '/feedback',
    query: { entity_type: 'call_instance', body: String(row['pattern'] || '') },
  });
}

void loadHealth();
</script>

<style scoped>
.libref-health-grid {
  max-width: 640px;
}
</style>
