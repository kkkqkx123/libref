<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.imports" :description="messages.importInsight.description" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item :label="messages.common.view">
          <el-radio-group v-model="view">
            <el-radio-button value="importers">{{ messages.importInsight.importers }}</el-radio-button>
            <el-radio-button value="imports">{{ messages.importInsight.imports }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="API">
          <el-input v-model="apiName" clearable  :placeholder="messages.importInsight.placeholder" class="libref-w-280"/>
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
      <EmptyState v-if="rows.length === 0" :description="messages.importInsight.empty" />
      <template v-else>
        <RowTable :rows="rows" />
        <div class="libref-pager">
          <el-button :disabled="offset === 0" @click="page(-1)">{{ messages.common.previous }}</el-button>
          <span class="libref-muted">{{ messages.common.offset(offset) }}</span>
          <el-button :disabled="rows.length < scopeStore.resultLimit" @click="page(1)">{{ messages.common.next }}</el-button>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElRadioButton, ElRadioGroup } from 'element-plus';
import { ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { ImportInsightRow } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import RowTable from '../components/common/RowTable.vue';
import { scopeParams, scopeStore } from '../stores/scope';
import {
  applyScopeFromQuery,
  readNumber,
  readString,
  scopeSnapshot,
  useQuerySync,
} from '../utils/query';

const route = useRoute();
const { sync } = useQuerySync();

applyScopeFromQuery(route.query);

type View = 'importers' | 'imports';
const VIEWS: View[] = ['importers', 'imports'];
const requested = readString(route.query.view) as View;
const view = ref<View>(VIEWS.includes(requested) ? requested : 'importers');
const apiName = ref(readString(route.query.api));
const offset = ref(readNumber(route.query.offset, 0));
const loading = ref(false);
const loaded = ref(false);
const error = ref('');
const requestId = ref('');
const rows = ref<ImportInsightRow[]>([]);

async function load(): Promise<void> {
  sync({
    view: view.value,
    api: apiName.value.trim() || undefined,
    limit: scopeStore.resultLimit,
    offset: offset.value,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api('/import-insight', {
      params: {
        view: view.value,
        api: apiName.value.trim() || undefined,
        ...scopeParams(),
        limit: scopeStore.resultLimit,
        offset: offset.value,
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

function page(direction: number): void {
  offset.value = Math.max(0, offset.value + direction * scopeStore.resultLimit);
  void load();
}

if (route.query.view || route.query.api) void load();
</script>

<style scoped>
.libref-pager {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin-top: var(--sp-3);
}
</style>
