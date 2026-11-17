<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.reader" :description="messages.reader.description" />
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent="load">
        <el-form-item :label="messages.reader.address">
          <el-input v-model="url" style="width: 320px" placeholder="https://…" />
        </el-form-item>
        <el-form-item :label="messages.common.mode">
          <el-radio-group v-model="mode">
            <el-radio-button label="page">{{ messages.reader.page }}</el-radio-button>
            <el-radio-button label="window">{{ messages.reader.window }}</el-radio-button>
            <el-radio-button label="section">{{ messages.reader.section }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="mode === 'window'" :label="messages.reader.position">
          <el-input-number v-model="sortOrder" :min="0" style="width: 120px" />
        </el-form-item>
        <el-form-item v-if="mode === 'window'" :label="messages.reader.radius">
          <el-input-number v-model="radius" :min="0" :max="20" style="width: 100px" />
        </el-form-item>
        <el-form-item v-if="mode === 'section'" :label="messages.reader.sourceGroup">
          <el-input v-model="sourceGroupId" style="width: 200px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" :disabled="!url" @click="load">{{ messages.common.open }}</el-button>
        </el-form-item>
      </el-form>
      <ScopeFilter />
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <template v-else-if="page">
      <div class="libref-card">
        <h3>{{ page.title || page.url }}</h3>
        <p class="libref-muted">{{ page.library }} {{ page.version }} {{ page.path }}</p>
        <pre class="libref-reader-body">{{ page.content }}</pre>
      </div>
      <div v-if="page.chunk_details.length > 0" class="libref-card">
        <h4>{{ messages.reader.chunkLocator }}（{{ page.chunks }}）</h4>
        <RowTable :rows="page.chunk_details" />
      </div>
      <div v-if="page.docs.length > 0" class="libref-card">
        <h4>{{ messages.reader.windowChunks }}</h4>
        <RowTable :rows="page.docs" />
      </div>
    </template>
    <EmptyState v-else :description="messages.reader.empty" />
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup } from 'element-plus';
import { ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { ReadResponse } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import RowTable from '../components/common/RowTable.vue';
import { notifyError } from '../stores/notify';
import { scopeParams, scopeStore } from '../stores/scope';

const route = useRoute();
const url = ref(typeof route.query.url === 'string' ? route.query.url : '');
const mode = ref<'page' | 'window' | 'section'>('page');
const sortOrder = ref(0);
const radius = ref(1);
const sourceGroupId = ref('');
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const page = ref<ReadResponse | null>(null);

if (typeof route.query.library === 'string') scopeStore.library = route.query.library;
if (typeof route.query.version === 'string') scopeStore.version = route.query.version;

async function load(): Promise<void> {
  if (!url.value) return;
  if (mode.value === 'window' && sortOrder.value === null) {
    notifyError(new Error(messages.value.reader.windowPositionRequired), messages.value.common.missingParam);
    return;
  }
  if (mode.value === 'section' && !sourceGroupId.value) {
    notifyError(new Error(messages.value.reader.sourceGroupRequired), messages.value.common.missingParam);
    return;
  }
  loading.value = true;
  error.value = '';
  page.value = null;
  try {
    const { data } = await api<ReadResponse>('/read', {
      params: {
        url: url.value,
        mode: mode.value,
        sort_order: mode.value === 'window' ? sortOrder.value : undefined,
        radius: mode.value === 'window' ? radius.value : undefined,
        source_group_id: mode.value === 'section' ? sourceGroupId.value : undefined,
        ...scopeParams(),
      },
    });
    page.value = data;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.reader.failed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

if (url.value) void load();
</script>

<style scoped>
.libref-reader-body {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.7;
  background: var(--surface-bg);
  border-radius: var(--radius-small);
  padding: 12px;
}
</style>
