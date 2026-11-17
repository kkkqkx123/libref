<template>
  <div class="libref-page">
    <PageHeader title="文库目录" description="已入库的文库与版本，入口可下钻到搜索与洞察" />
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="生态">
          <el-input v-model="ecosystem" clearable placeholder="如 npm" style="width: 160px" @change="reload" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="reload">刷新</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="libraries.length === 0" description="尚未入库任何文库，先走入库向导" />
      <el-table v-else :data="libraries" stripe style="width: 100%" @row-click="openLibraryRow">
        <el-table-column prop="name" label="文库" />
        <el-table-column prop="ecosystem" label="生态" width="120" />
        <el-table-column prop="versions" label="版本数" width="100" />
        <el-table-column prop="description" label="描述" show-overflow-tooltip />
        <el-table-column label="操作" width="220">
          <template #default="scope">
            <el-button link type="primary" size="small" @click.stop="drill(asLibrary(scope.row), 'search')">搜索</el-button>
            <el-button link type="primary" size="small" @click.stop="drill(asLibrary(scope.row), 'answer')">问答</el-button>
            <el-button link type="primary" size="small" @click.stop="toggleVersions(asLibrary(scope.row))">
              {{ expanded === asLibrary(scope.row).name ? '收起版本' : '查看版本' }}
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
        <el-table-column prop="version" label="版本" />
        <el-table-column prop="ecosystem" label="生态" width="120" />
        <el-table-column prop="pages" label="页数" width="100" />
        <el-table-column prop="registry_confirmed" label="注册表确认" width="140">
          <template #default="scope">{{ scope.row.registry_confirmed ? '是' : '否' }}</template>
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
    error.value = err instanceof ApiError ? err.message : '加载失败';
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
    notifyError(err, '加载版本失败');
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
