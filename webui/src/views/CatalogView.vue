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
        <el-table-column :label="messages.common.actions" width="320">
          <template #default="scope">
            <el-button link type="primary" size="small" @click.stop="drill(asLibrary(scope.row), 'search')">{{ messages.common.search }}</el-button>
            <el-button link type="primary" size="small" @click.stop="drill(asLibrary(scope.row), 'answer')">{{ messages.common.answer }}</el-button>
            <el-dropdown @command="(path: string) => drillInsight(asLibrary(scope.row), path)">
              <el-button link type="primary" size="small">{{ messages.catalog.insights }}</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="/insights/api">{{ messages.nav.apiInsight }}</el-dropdown-item>
                  <el-dropdown-item command="/insights/calls">{{ messages.nav.calls }}</el-dropdown-item>
                  <el-dropdown-item command="/insights/patterns">{{ messages.nav.patterns }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button link type="primary" size="small" @click.stop="toggleVersions(asLibrary(scope.row))">
              {{ expanded === asLibrary(scope.row).name ? messages.catalog.collapseVersions : messages.catalog.viewVersions }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <template v-if="expanded && currentVersions.length > 0">
        <div class="libref-version-bar">
          <span class="libref-muted">{{ messages.catalog.versionsOf(expanded, currentVersions.length) }}</span>
          <el-checkbox v-model="stableOnly">{{ messages.catalog.stableOnly }}</el-checkbox>
        </div>
        <EmptyState v-if="visibleVersions.length === 0" :description="messages.catalog.onlyPrerelease" />
        <el-table v-else :data="visibleVersions" stripe style="width: 100%; margin-top: 8px">
          <el-table-column prop="version" :label="messages.common.version">
            <template #default="scope">
              <span class="libref-mono">{{ scope.row.version }}</span>
              <el-tag v-if="scope.row.prerelease" size="small" type="warning" class="libref-prerelease-tag">
                {{ messages.catalog.prerelease }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="ecosystem" :label="messages.common.ecosystem" width="120" />
          <el-table-column prop="pages" :label="messages.catalog.pages" width="100" />
          <el-table-column prop="registry_confirmed" :label="messages.catalog.registryConfirmed" width="140">
            <template #default="scope">{{ scope.row.registry_confirmed ? messages.common.yes : messages.common.no }}</template>
          </el-table-column>
          <el-table-column :label="messages.common.actions" width="180">
            <template #default="scope">
              <el-button
                link
                type="primary"
                size="small"
                :disabled="scope.row.pages === 0"
                @click.stop="openPages(asVersion(scope.row))"
              >
                {{ messages.catalog.viewPages }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </template>

    <el-dialog v-model="pagesOpen" :title="pagesTitle" width="640px" @closed="resetPages">
      <LoadingPanel v-if="pagesLoading" />
      <ErrorState v-else-if="pagesError" :message="pagesError" :request-id="pagesRequestId" @retry="loadPages" />
      <template v-else>
        <EmptyState v-if="pageRows.length === 0" :description="messages.catalog.pagesEmpty" />
        <el-table v-else :data="pageRows" stripe style="width: 100%" @row-click="openPage">
          <el-table-column prop="title" :label="messages.catalog.pageTitle" show-overflow-tooltip>
            <template #default="scope">{{ scope.row.title || scope.row.url }}</template>
          </el-table-column>
          <el-table-column prop="chunks" :label="messages.catalog.pageChunks" width="100" />
          <el-table-column :label="messages.common.actions" width="120">
            <template #default="scope">
              <el-button link type="primary" size="small" @click.stop="openPage(scope.row)">
                {{ messages.catalog.openInReader }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="pageTotal > pageLimit" class="libref-pager">
          <el-button size="small" :disabled="pageOffset === 0" @click="pageBy(-1)">
            {{ messages.catalog.previousPage }}
          </el-button>
          <span class="libref-muted">{{ messages.catalog.pageRange(pageOffset + 1, Math.min(pageOffset + pageLimit, pageTotal), pageTotal) }}</span>
          <el-button size="small" :disabled="pageOffset + pageLimit >= pageTotal" @click="pageBy(1)">
            {{ messages.catalog.nextPage }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCheckbox, ElDialog, ElDropdown, ElDropdownItem, ElDropdownMenu, ElForm, ElFormItem, ElInput, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, LibraryRow, PageList, PageRow, VersionRow } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import { notifyError } from '../stores/notify';
import { scopeStore, setLibrary } from '../stores/scope';
import { readString, useQuerySync } from '../utils/query';

const route = useRoute();
const router = useRouter();
const { sync } = useQuerySync();
const ecosystem = ref(readString(route.query.ecosystem));
const libraries = ref<LibraryRow[]>([]);
const currentVersions = ref<VersionRow[]>([]);
const expanded = ref('');
const loading = ref(false);
const error = ref('');
const requestId = ref('');
const stableOnly = ref(false);

const visibleVersions = computed(() =>
  stableOnly.value ? currentVersions.value.filter((row) => !row.prerelease) : currentVersions.value,
);

// Page picker: the reader is URL-addressed, so this is how a version turns into
// a readable page without guessing addresses.
const pagesOpen = ref(false);
const pagesLoading = ref(false);
const pagesError = ref('');
const pagesRequestId = ref('');
const pageRows = ref<PageRow[]>([]);
const pageTotal = ref(0);
const pageLimit = 50;
const pageOffset = ref(0);
const pageLibrary = ref<LibraryRow | null>(null);
const pageVersion = ref<VersionRow | null>(null);

const pagesTitle = computed(() => {
  const lib = pageLibrary.value?.name || '';
  const ver = pageVersion.value?.version || '';
  return messages.value.catalog.pagesOf(lib, ver);
});

async function reload(): Promise<void> {
  sync({ ecosystem: ecosystem.value });
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

function asVersion(row: unknown): VersionRow {
  return row as VersionRow;
}

function openPages(version: VersionRow): void {
  const library = libraries.value.find((row) => row.name === expanded.value) || null;
  pageLibrary.value = library;
  pageVersion.value = version;
  pageOffset.value = 0;
  pageRows.value = [];
  pagesOpen.value = true;
  void loadPages();
}

async function loadPages(): Promise<void> {
  const library = pageLibrary.value;
  const version = pageVersion.value;
  if (!library || !version) return;
  pagesLoading.value = true;
  pagesError.value = '';
  try {
    const { data } = await api<PageList>('/pages', {
      params: {
        library: library.name,
        version: version.version,
        ecosystem: library.ecosystem || undefined,
        limit: pageLimit,
        offset: pageOffset.value,
      },
    });
    pageRows.value = data.items;
    pageTotal.value = data.total;
  } catch (err) {
    pagesError.value = err instanceof ApiError ? err.message : messages.value.catalog.pagesFailed;
    pagesRequestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    pagesLoading.value = false;
  }
}

function pageBy(direction: number): void {
  pageOffset.value = Math.max(0, pageOffset.value + direction * pageLimit);
  void loadPages();
}

function resetPages(): void {
  pageRows.value = [];
  pageTotal.value = 0;
  pagesError.value = '';
  pagesRequestId.value = '';
}

function openPage(row: unknown): void {
  const page = row as PageRow;
  const library = pageLibrary.value;
  const version = pageVersion.value;
  if (!page.url) return;
  void router.push({
    path: '/reader',
    query: {
      url: page.url,
      mode: 'page',
      library: library?.name || undefined,
      version: version?.version || undefined,
      ecosystem: library?.ecosystem || undefined,
    },
  });
}

function drillInsight(row: LibraryRow, path: string): void {
  setLibrary(row.name);
  scopeStore.ecosystem = row.ecosystem || '';
  void router.push({ path, query: { library: row.name, ecosystem: row.ecosystem || undefined } });
}

void reload();
</script>

<style scoped>
.libref-version-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
}
.libref-prerelease-tag {
  margin-left: 6px;
}
.libref-pager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}
</style>
