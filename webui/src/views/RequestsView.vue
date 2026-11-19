<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.requests" :description="messages.requests.description" />
    <div class="libref-card">
      <el-form :model="form" label-width="130px" @submit.prevent="submit">
        <el-form-item :label="messages.common.kind">
          <el-radio-group v-model="form.kind">
            <el-radio-button value="repo">{{ messages.common.repo }}</el-radio-button>
            <el-radio-button value="docs">{{ messages.common.docs }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="messages.common.library">
          <el-input v-model="form.library" :placeholder="messages.requests.libraryPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="form.version" :placeholder="messages.requests.versionPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.common.ecosystem">
          <el-input v-model="form.ecosystem" />
        </el-form-item>
        <el-form-item v-if="form.kind === 'repo'" :label="messages.requests.source">
          <el-input v-model="form.source" :placeholder="messages.requests.sourcePlaceholder" />
        </el-form-item>
        <el-form-item v-else :label="messages.requests.startUrl">
          <el-input v-model="form.startUrl" :placeholder="messages.requests.startUrlPlaceholder" />
        </el-form-item>
        <el-form-item :label="messages.requests.advanced">
          <el-switch v-model="showAdvanced" />
        </el-form-item>
        <template v-if="showAdvanced">
          <el-form-item v-if="form.kind === 'docs'" :label="messages.ingest.maxPages">
            <el-input-number v-model="form.max_pages" :min="1" />
          </el-form-item>
          <el-form-item v-if="form.kind === 'docs'" :label="messages.requests.prefilter">
            <el-checkbox v-model="form.prefilter" />
          </el-form-item>
          <el-form-item v-if="form.kind === 'repo'" :label="messages.ingest.probeRepoSubdir">
            <el-input v-model="form.subdir" placeholder="packages/core" />
          </el-form-item>
          <el-form-item :label="messages.requests.maxFiles">
            <el-input-number v-model="form.max_files" :min="1" />
          </el-form-item>
          <el-form-item :label="messages.requests.onlyFiles">
            <TagInput v-model="form.only_files" :placeholder="messages.common.tagPlaceholder" />
          </el-form-item>
          <el-form-item :label="messages.requests.excludePatterns">
            <TagInput v-model="form.exclude_patterns" :placeholder="messages.common.tagPlaceholder" />
          </el-form-item>
          <el-form-item :label="messages.requests.update">
            <el-checkbox v-model="form.update" />
          </el-form-item>
          <el-form-item :label="messages.requests.fresh">
            <el-checkbox v-model="form.fresh" />
          </el-form-item>
          <el-form-item :label="messages.common.includePrerelease">
            <el-checkbox v-model="scopeStore.includePrerelease" />
          </el-form-item>
        </template>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submit">{{ messages.common.submit }}</el-button>
          <el-button :loading="probing" @click="probe">{{ messages.common.probe }}</el-button>
        </el-form-item>
      </el-form>
      <div v-if="probeResult" class="libref-muted">{{ probeResult }}</div>
    </div>

    <LoadingPanel v-if="loading && items.length === 0" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="reload" />
    <template v-else>
      <EmptyState v-if="items.length === 0" :description="messages.requests.empty" />
      <div v-for="item in items" :key="item.id" class="libref-card libref-request-row">
        <div class="libref-review-main">
          <StatusBadge :status="item.status" />
          <span class="libref-mono">#{{ item.id }} {{ item.kind }}</span>
          <span>{{ item.target }}</span>
        </div>
        <div class="libref-muted">
          {{ item.requester || messages.common.unknown }} · {{ item.created_at }} ·
          {{ messages.requests.job }} {{ item.job_id ?? messages.common.none }}
        </div>
        <div v-if="item.reject_reason || item.error" class="libref-muted">
          {{ item.reject_reason || item.error }}
        </div>
        <div class="libref-review-actions">
          <el-button size="small" :disabled="!['pending', 'approved', 'queued'].includes(item.status)" @click="cancel(item)">
            {{ messages.common.cancel }}
          </el-button>
          <el-button size="small" @click="reapply(item)">{{ messages.requests.reapply }}</el-button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import {
  ElButton, ElCheckbox, ElForm, ElFormItem, ElInput, ElInputNumber,
  ElRadioButton, ElRadioGroup, ElSwitch,
} from 'element-plus';
import { ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, ApiError } from '../api/client';
import type {
  DocProbeRequest,
  IndexRequest,
  IndexRequestCreate,
  IndexRequestParams,
  RepoProbeRequest,
} from '../api/types';
import { readString } from '../utils/query';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import StatusBadge from '../components/common/StatusBadge.vue';
import TagInput from '../components/common/TagInput.vue';
import { messages } from '../i18n';
import { notifyError, notifySuccess } from '../stores/notify';
import { scopeStore } from '../stores/scope';

const route = useRoute();
const presetKind = readString(route.query.kind);
const form = ref({
  kind: (presetKind === 'docs' ? 'docs' : 'repo') as 'repo' | 'docs',
  library: readString(route.query.library),
  version: readString(route.query.version),
  ecosystem: readString(route.query.ecosystem),
  source: readString(route.query.source),
  startUrl: readString(route.query.start_url),
  max_pages: undefined as number | undefined,
  prefilter: false,
  subdir: '',
  max_files: undefined as number | undefined,
  only_files: [] as string[],
  exclude_patterns: [] as string[],
  update: false,
  fresh: false,
});
const showAdvanced = ref(false);
const items = ref<IndexRequest[]>([]);
const loading = ref(false);
const submitting = ref(false);
const probing = ref(false);
const error = ref('');
const requestId = ref('');
const probeResult = ref('');

async function reload(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api('/index-requests', { params: { limit: 50 } });
    items.value = data.items;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.common.loadFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function payload(): IndexRequestCreate {
  const base: IndexRequestCreate = {
    kind: form.value.kind,
    library: form.value.library,
    version: form.value.version,
    prefilter: form.value.prefilter,
    update: form.value.update,
    fresh: form.value.fresh,
    include_prerelease: scopeStore.includePrerelease,
  };
  if (form.value.ecosystem) base.ecosystem = form.value.ecosystem;
  if (form.value.kind === 'repo') {
    base.source = form.value.source;
    if (form.value.subdir) base.subdir = form.value.subdir;
  } else {
    base.start_url = form.value.startUrl;
    if (form.value.max_pages) base.max_pages = form.value.max_pages;
  }
  if (form.value.max_files) base.max_files = form.value.max_files;
  if (form.value.only_files.length > 0) base.only_files = form.value.only_files;
  if (form.value.exclude_patterns.length > 0) base.exclude_patterns = form.value.exclude_patterns;
  return base;
}

async function submit(): Promise<void> {
  submitting.value = true;
  try {
    const { data } = await api('/index-requests', { method: 'POST', body: payload() });
    notifySuccess(messages.value.requests.submitted(String(data.id)));
    await reload();
  } catch (err) {
    notifyError(err, messages.value.requests.submitFailed);
  } finally {
    submitting.value = false;
  }
}

async function probe(): Promise<void> {
  probing.value = true;
  probeResult.value = '';
  try {
    if (form.value.kind === 'repo') {
      const body: RepoProbeRequest = { source: form.value.source };
      if (form.value.subdir) body.subdir = form.value.subdir;
      if (form.value.max_files) body.max_files = form.value.max_files;
      if (form.value.only_files.length > 0) body.only_files = form.value.only_files;
      if (form.value.exclude_patterns.length > 0) body.exclude_patterns = form.value.exclude_patterns;
      const { data } = await api('/repo-probe', {
        method: 'POST',
        body,
      });
      probeResult.value = messages.value.requests.probeResult(data.root_kind, data.effective_files);
    } else {
      const body: DocProbeRequest = {
        start_url: form.value.startUrl,
        prefilter: form.value.prefilter,
      };
      if (form.value.max_pages) body.max_pages = form.value.max_pages;
      const { data } = await api('/doc-probe', {
        method: 'POST',
        body,
      });
      probeResult.value = messages.value.requests.probeResult('docs', data.accepted);
    }
  } catch (err) {
    notifyError(err, messages.value.requests.probeFailed);
  } finally {
    probing.value = false;
  }
}

async function cancel(item: IndexRequest): Promise<void> {
  try {
    await api('/index-requests/{request_id}/cancel', {
      method: 'POST',
      pathParams: { request_id: item.id },
    });
    await reload();
  } catch (err) {
    notifyError(err, messages.value.requests.cancelFailed);
  }
}

function reapply(item: IndexRequest): void {
  const params: IndexRequestParams = item.params;
  const kind = params.kind || item.kind;
  form.value.kind = kind;
  form.value.library = params.library;
  form.value.version = params.version;
  form.value.ecosystem = params.ecosystem || '';
  form.value.source = kind === 'repo' ? params.source || '' : '';
  form.value.startUrl = kind === 'docs' ? params.start_url || '' : '';
  form.value.max_pages = params.max_pages ?? undefined;
  form.value.prefilter = params.prefilter;
  form.value.subdir = params.subdir || '';
  form.value.max_files = params.max_files ?? undefined;
  form.value.only_files = params.only_files || [];
  form.value.exclude_patterns = params.exclude_patterns || [];
  form.value.update = params.update;
  form.value.fresh = params.fresh;
  if (typeof params.include_prerelease === 'boolean') scopeStore.includePrerelease = params.include_prerelease;
  showAdvanced.value = true;
}

void reload();
</script>

<style scoped>
.libref-request-row { margin-bottom: var(--sp-2); }
</style>
