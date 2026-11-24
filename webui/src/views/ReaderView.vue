<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.reader" :description="messages.reader.description" />
    <div class="libref-card">
      <el-form :inline="true" @submit.prevent="load">
        <el-form-item :label="messages.reader.address">
          <el-input v-model="url" placeholder="https://…" class="libref-w-320"/>
        </el-form-item>
        <el-form-item :label="messages.common.mode">
          <el-radio-group v-model="mode">
            <el-radio-button value="page">{{ messages.reader.page }}</el-radio-button>
            <el-radio-button value="window">{{ messages.reader.window }}</el-radio-button>
            <el-radio-button value="section">{{ messages.reader.section }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="mode === 'window'" :label="messages.reader.position">
          <el-input-number v-model="sortOrder" :min="0" class="libref-w-120"/>
        </el-form-item>
        <el-form-item v-if="mode === 'window'" :label="messages.reader.radius">
          <el-input-number v-model="radius" :min="0" :max="20" class="libref-w-100"/>
        </el-form-item>
        <el-form-item v-if="mode === 'section'" :label="messages.reader.sourceGroup">
          <el-input v-model="sourceGroupId" class="libref-w-200"/>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" :disabled="!url" @click="load">{{ messages.common.open }}</el-button>
        </el-form-item>
      </el-form>
      <ScopeFilter />
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <EmptyState v-else-if="notFound" :description="messages.reader.notFoundHint">
      <div class="libref-reader-missing">
        <p>{{ messages.reader.notFound }}</p>
        <el-button type="primary" @click="goIngest">{{ messages.reader.goIngest }}</el-button>
      </div>
      <div v-if="candidates.length > 0" class="libref-reader-candidates">
        <p class="libref-muted">{{ messages.reader.candidateHint(candidates.length) }}</p>
        <ul class="libref-note-list">
          <li v-for="row in candidates" :key="row.url">
            <el-button link type="primary" @click="openCandidate(row)">
              {{ row.title || row.url }}
            </el-button>
          </li>
        </ul>
      </div>
    </EmptyState>
    <template v-else-if="page">
      <div v-if="mode === 'window'" class="libref-reader-nav">
        <el-button size="small" :disabled="previousOrder === null" @click="jump(previousOrder)">
          {{ messages.reader.previousWindow }}
        </el-button>
        <el-button size="small" :disabled="nextOrder === null" @click="jump(nextOrder)">
          {{ messages.reader.nextWindow }}
        </el-button>
      </div>
      <div class="libref-card">
        <h3>{{ page.title || page.url }}</h3>
        <p class="libref-muted">{{ page.library }} {{ page.version }} {{ page.path }}</p>
        <pre class="libref-reader-body">{{ page.content }}</pre>
      </div>
      <div v-if="docInsightEntries.length > 0" class="libref-card">
        <h4>{{ messages.reader.docFeatures }}</h4>
        <KeyValue :entries="docInsightEntries" />
      </div>
      <div v-if="(page.chunk_details?.length || 0) > 0" class="libref-card">
        <h4>{{ messages.reader.chunkLocatorCount(page.chunks || 0) }}</h4>
        <RowTable :rows="page.chunk_details || []" :columns="LOCATOR_COLUMNS" />
      </div>
      <div v-if="(page.docs?.length || 0) > 0" class="libref-card">
        <h4>{{ messages.reader.windowChunks }}</h4>
        <RowTable :rows="page.docs || []" :columns="DOC_COLUMNS" />
      </div>
    </template>
    <EmptyState v-else :description="messages.reader.empty" />
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup } from 'element-plus';
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import { field, type DocInsightRow, type PageRow, type ReadResponse } from '../api/types';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import RowTable from '../components/common/RowTable.vue';
import { notifyError } from '../stores/notify';
import { scopeParams } from '../stores/scope';
import {
  applyScopeFromQuery,
  readNumber,
  readString,
  scopeSnapshot,
  useQuerySync,
} from '../utils/query';

type Mode = 'page' | 'window' | 'section';

const LOCATOR_COLUMNS = ['chunk_index', 'total_chunks', 'span_kind', 'length'] as const;
const DOC_COLUMNS = ['url', 'chunk_index', 'total_chunks', 'span_kind', 'length'] as const;

function readMode(value: string): Mode {
  return value === 'window' || value === 'section' ? value : 'page';
}

const route = useRoute();
const router = useRouter();
const { sync } = useQuerySync();

applyScopeFromQuery(route.query);
const url = ref(readString(route.query.url));
const mode = ref<Mode>(readMode(readString(route.query.mode)));
const sortOrder = ref(readNumber(route.query.sort_order, 0));
const radius = ref(readNumber(route.query.radius, 1));
const sourceGroupId = ref(readString(route.query.source_group_id));
const loading = ref(false);
const error = ref('');
const notFound = ref(false);
const requestId = ref('');
const page = ref<ReadResponse | null>(null);
// Candidates only appear on a 404 with a pinned library + version: without one the
// inventory would be an arbitrary slice of the whole catalogue.
const candidates = ref<PageRow[]>([]);
const docInsight = ref<{ entities: string[]; patterns: string[]; nodes: string[] }>({
  entities: [],
  patterns: [],
  nodes: [],
});

const docInsightEntries = computed<KeyValueEntry[]>(() => {
  const entries: KeyValueEntry[] = [];
  if (docInsight.value.entities.length > 0) {
    entries.push({
      label: messages.value.reader.alignedEntities,
      value: docInsight.value.entities.join(', '),
    });
  }
  if (docInsight.value.patterns.length > 0) {
    entries.push({
      label: messages.value.reader.coveredPatterns,
      value: docInsight.value.patterns.join(', '),
    });
  }
  if (docInsight.value.nodes.length > 0) {
    entries.push({
      label: messages.value.reader.featureNodes,
      value: docInsight.value.nodes.join(', '),
    });
  }
  return entries;
});

function stringsOf(row: DocInsightRow, key: string): string[] {
  const value = field(row, key);
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

async function loadDocInsight(data: ReadResponse): Promise<void> {
  docInsight.value = { entities: [], patterns: [], nodes: [] };
  const scope = scopeParams();
  const library = String(data.library || scope.library || '');
  const version = String(data.version || scope.version || '');
  const ids = [...(data.chunk_details || []), ...(data.docs || [])]
    .map((chunk) => chunk.id)
    .filter((id): id is number => typeof id === 'number' && id > 0);
  if (!library || !version || ids.length === 0) return;
  try {
    const unique = [...new Set(ids)].slice(0, 8);
    const responses = await Promise.all(
      unique.flatMap((documentId) =>
        (['links', 'coverage'] as const).map((view) =>
          api('/doc-insight', {
            params: {
              view,
              library,
              version,
              ecosystem: typeof scope.ecosystem === 'string' ? scope.ecosystem : undefined,
              document_id: documentId,
              limit: 50,
            },
          }),
        ),
      ),
    );
    const entities = new Set<string>();
    const patterns = new Set<string>();
    const nodes = new Set<string>();
    for (const response of responses) {
      for (const row of response.data.items) {
        for (const entity of stringsOf(row, 'api')) entities.add(entity);
        for (const pattern of stringsOf(row, 'pattern')) patterns.add(pattern);
        for (const node of stringsOf(row, 'feature_nodes')) nodes.add(node);
      }
    }
    docInsight.value = {
      entities: [...entities].sort(),
      patterns: [...patterns].sort(),
      nodes: [...nodes].sort(),
    };
  } catch {
    // Derived insight is optional; the reader keeps showing the source text.
    docInsight.value = { entities: [], patterns: [], nodes: [] };
  }
}

function pathOf(value: string): string {
  try {
    return new URL(value).pathname;
  } catch {
    return value;
  }
}

function sharedPrefix(left: string, right: string): number {
  let index = 0;
  while (index < left.length && index < right.length && left[index] === right[index]) index += 1;
  return index;
}

async function loadCandidates(target: string): Promise<void> {
  const scope = scopeParams();
  const library = String(scope.library || '');
  const version = String(scope.version || '');
  candidates.value = [];
  if (!library || !version) return;
  try {
    const { data } = await api('/pages', {
      params: {
        library,
        version,
        ecosystem: typeof scope.ecosystem === 'string' ? scope.ecosystem : undefined,
        limit: 20,
      },
    });
    const base = pathOf(target);
    // Ordering only: pages sharing more of the missed path come first. No scoring
    // rules are copied from the backend search.
    candidates.value = [...data.items]
      .sort((a, b) => sharedPrefix(pathOf(b.url), base) - sharedPrefix(pathOf(a.url), base))
      .slice(0, 5);
  } catch {
    // A failed enumeration must not replace the primary guidance.
    candidates.value = [];
  }
}

function openCandidate(row: PageRow): void {
  if (!row.url) return;
  url.value = row.url;
  mode.value = 'page';
  void load();
}

const deliveredOrders = computed<number[]>(() =>
  (page.value?.docs || [])
    .map((chunk) => Number(chunk.sort_order))
    .filter((order) => Number.isFinite(order)),
);

const previousOrder = computed<number | null>(() => {
  if (deliveredOrders.value.length === 0) return null;
  return Math.max(0, Math.min(...deliveredOrders.value) - 1);
});

const nextOrder = computed<number | null>(() => {
  if (deliveredOrders.value.length === 0) return null;
  return Math.max(...deliveredOrders.value) + 1;
});

function jump(order: number | null): void {
  if (order === null) return;
  sortOrder.value = order;
  void load();
}

function goIngest(): void {
  void router.push('/ingest');
}

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
  sync({
    url: url.value,
    mode: mode.value,
    sort_order: mode.value === 'window' ? sortOrder.value : undefined,
    radius: mode.value === 'window' ? radius.value : undefined,
    source_group_id: mode.value === 'section' ? sourceGroupId.value : undefined,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  notFound.value = false;
  page.value = null;
  try {
    const { data } = await api('/read', {
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
    void loadDocInsight(data);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound.value = true;
      void loadCandidates(url.value);
      return;
    }
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
  font-size: var(--fs-base);
  line-height: 1.7;
  background: var(--surface-bg);
  border-radius: var(--radius-small);
  padding: var(--sp-3);
}
.libref-reader-nav {
  display: flex;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.libref-reader-missing {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  align-items: center;
}
.libref-reader-candidates {
  margin-top: var(--sp-4);
  text-align: left;
}
</style>
