<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.catalog" :description="messages.catalog.description" />
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item :label="messages.common.ecosystem">
          <el-input v-model="ecosystem" clearable  :placeholder="messages.catalog.ecosystemPlaceholder" style="width: 160px" @change="reload" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="reload">{{ messages.common.refresh }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="libraries.length === 0" :description="messages.catalog.empty" />
      <el-table v-else :data="libraries" stripe style="width: 100%" @row-click="openLibraryRow">
        <el-table-column prop="name" :label="messages.catalog.library" />
        <el-table-column prop="ecosystem" :label="messages.common.ecosystem" width="120" />
        <el-table-column prop="versions" :label="messages.catalog.versions" width="100" />
        <el-table-column prop="description" :label="messages.catalog.descriptionCol" show-overflow-tooltip />
        <el-table-column :label="messages.common.actions" width="220">
          <template #default="scope">
            <el-button link type="primary" size="small" @click.stop="drill(asLibrary(scope.row), 'search')">{{ messages.common.search }}</el-button>
            <el-button link type="primary" size="small" @click.stop="drill(asLibrary(scope.row), 'answer')">{{ messages.common.answer }}</el-button>
            <el-button link type="primary" size="small" @click.stop="toggleVersions(asLibrary(scope.row))">
              {{ expanded === asLibrary(scope.row).name ? messages.catalog.collapseVersions : messages.catalog.viewVersions }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-table
        v-if="expanded && currentVersions.length > 0"
        :data="currentVersions"
        stripe
        style="width: 100%; margin-top: 12px"
      >
        <el-table-column prop="version" :label="messages.common.version" />
        <el-table-column prop="ecosystem" :label="messages.common.ecosystem" width="120" />
        <el-table-column prop="pages" :label="messages.catalog.pages" width="100" />
        <el-table-column prop="registry_confirmed" :label="messages.catalog.registryConfirmed" width="140">
          <template #default="scope">{{ scope.row.registry_confirmed ? messages.common.yes : messages.common.no }}</template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElTable, ElTableColumn } from 'element-plus';
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, LibraryRow, VersionRow } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import { notifyError } from '../stores/notify';
import { scopeStore, setLibrary } from '../stores/scope';

const router = useRouter();
const ecosystem = ref('');
const libraries = ref<LibraryRow[]>([]);
const currentVersions = ref<VersionRow[]>([]);
const expanded = ref('');
const loading = ref(false);
const error = ref('');
const requestId = ref('');

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<LibraryRow>>('/catalog', {
      params: { ecosystem: ecosystem.value || undefined },
    });
    libraries.value = data.items;
    expanded.value = '';
    currentVersions.value = [];
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

async function toggleVersions(row: LibraryRow): Promise<void> {
  if (expanded.value === row.name) {
    expanded.value = '';
    currentVersions.value = [];
    return;
  }
  try {
    const { data } = await api<Envelope<VersionRow>>('/catalog', {
      params: { library: row.name, ecosystem: row.ecosystem || undefined },
    });
    currentVersions.value = data.items;
    expanded.value = row.name;
  } catch (err) {
    notifyError(err, messages.value.catalog.loadVersionsFailed);
  }
}

function asLibrary(row: unknown): LibraryRow {
  return row as LibraryRow;
}

function openLibraryRow(row: unknown): void {
  void toggleVersions(asLibrary(row));
}

function drill(row: LibraryRow, target: 'search' | 'answer'): void {
  setLibrary(row.name);
  scopeStore.ecosystem = row.ecosystem || '';
  void router.push(`/${target}`);
}

void reload();
</script>
