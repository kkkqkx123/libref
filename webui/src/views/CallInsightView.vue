<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.calls" :description="messages.callInsight.description" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item :label="messages.common.view">
          <el-radio-group v-model="view">
            <el-radio-button value="callee">{{ messages.callInsight.callee }}</el-radio-button>
            <el-radio-button value="caller">{{ messages.callInsight.caller }}</el-radio-button>
            <el-radio-button value="file">{{ messages.common.file }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="API">
          <el-input v-model="apiName" clearable  :placeholder="messages.callInsight.apiPlaceholder" class="libref-w-280"/>
        </el-form-item>
        <el-form-item :label="messages.callInsight.filter">
          <el-checkbox v-model="includeAmbiguous">{{ messages.callInsight.includeAmbiguous }}</el-checkbox>
        </el-form-item>
        <el-form-item :label="messages.callInsight.resolveKinds">
          <TagInput v-model="resolveKinds"  :placeholder="messages.callInsight.resolveKindsPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.callInsight.argCount">
          <el-input-number v-model="minArgs" :min="0"  :placeholder="messages.common.min" class="libref-w-110"/>
          <span class="libref-muted">—</span>
          <el-input-number v-model="maxArgs" :min="0"  :placeholder="messages.common.max" class="libref-w-110"/>
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
      <EmptyState v-if="rows.length === 0" :description="messages.callInsight.empty" />
      <template v-else>
        <EvidenceList
          :rows="rows"
          :inspect-label="messages.common.viewSymbol"
          @inspect="forwardSymbol"
          @feedback="forwardFeedback"
        />
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
import { ElButton, ElCheckbox, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup } from 'element-plus';
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import { field, type CallInsightRow, type Row } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EvidenceList from '../components/business/EvidenceList.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import TagInput from '../components/common/TagInput.vue';
import { scopeParams, scopeStore } from '../stores/scope';
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
type View = 'callee' | 'caller' | 'file';
const VIEWS: View[] = ['callee', 'caller', 'file'];
const requested = readString(route.query.view) as View;
const view = ref<View>(VIEWS.includes(requested) ? requested : 'callee');
const apiName = ref(readString(route.query.api));
const includeAmbiguous = ref(readBool(route.query.include_ambiguous, false));
const resolveKinds = ref<string[]>(readList(route.query.resolve_kinds));
const minArgs = ref<number | undefined>(
  route.query.min_args ? readNumber(route.query.min_args, 0) : undefined,
);
const maxArgs = ref<number | undefined>(
  route.query.max_args ? readNumber(route.query.max_args, 0) : undefined,
);
const offset = ref(readNumber(route.query.offset, 0));
const loading = ref(false);
const loaded = ref(false);
const error = ref('');
const requestId = ref('');
const rows = ref<CallInsightRow[]>([]);

async function load(): Promise<void> {
  sync({
    view: view.value,
    api: apiName.value.trim(),
    include_ambiguous: includeAmbiguous.value,
    resolve_kinds: resolveKinds.value,
    min_args: minArgs.value,
    max_args: maxArgs.value,
    limit: scopeStore.resultLimit,
    offset: offset.value,
    ...scopeSnapshot(),
  });
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api('/call-insight', {
      params: {
        view: view.value,
        api: apiName.value.trim() || undefined,
        ...scopeParams(),
        limit: scopeStore.resultLimit,
        offset: offset.value,
        include_ambiguous: includeAmbiguous.value,
        resolve_kinds: resolveKinds.value,
        min_args: minArgs.value,
        max_args: maxArgs.value,
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

function forwardSymbol(row: Row): void {
  const api = field(row, 'callee');
  void router.push({
    path: '/insights/symbols',
    query: { view: 'detail', api: typeof api === 'string' ? api : undefined },
  });
}

function forwardFeedback(row: Row): void {
  const entityId = Number(field(row, 'entity_id'));
  void router.push({
    path: '/feedback',
    query: {
      entity_type: 'call_instance',
      entity_id: Number.isFinite(entityId) && entityId > 0 ? String(entityId) : undefined,
    },
  });
}
</script>

<style scoped>
.libref-pager {
  display: flex;
  gap: var(--sp-3);
  align-items: center;
  margin-top: var(--sp-3);
}
</style>
