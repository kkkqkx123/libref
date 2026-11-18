<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.ingest" :description="messages.ingest.description" />
    <QuotaNotice v-if="quotaReset !== null" :reset-at="quotaReset">
      <el-button size="small" type="primary" :loading="busy" @click="retryQuota">
        {{ messages.common.retry }}
      </el-button>
    </QuotaNotice>
    <el-steps :active="step" finish-status="success" class="libref-steps">
      <el-step :title="messages.ingest.stepResolve" />
      <el-step :title="messages.ingest.stepProbe" />
      <el-step :title="messages.ingest.stepSubmit" />
    </el-steps>

    <div v-if="step === 0" class="libref-card">
      <h4>{{ messages.ingest.step1 }}</h4>
      <el-form @submit.prevent="resolve">
        <el-form-item :label="messages.common.ecosystem">
          <el-input v-model="form.ecosystem" :placeholder="messages.ingest.ecosystemPlaceholder" style="width: 200px" />
        </el-form-item>
        <el-form-item :label="messages.common.name">
          <el-input v-model="form.name" :placeholder="messages.ingest.namePlaceholder" style="width: 280px" />
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="form.version" :placeholder="messages.ingest.versionPlaceholder" style="width: 200px" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="scopeStore.includePrerelease">{{ messages.common.includePrerelease }}</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="busy" :disabled="!form.name.trim()" @click="resolve">
            {{ messages.common.parse }}
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="resolution" class="libref-resolution">
        <KeyValue :entries="resolutionEntries" />
        <div v-if="repoRedirect" class="libref-repo-redirect">
          <p class="libref-muted">{{ messages.ingest.repoRedirectHint }}</p>
          <el-button type="primary" @click="goRequests">{{ messages.ingest.goRequests }}</el-button>
        </div>
        <el-button v-else type="primary" @click="step = 1">{{ messages.ingest.stepToProbe }}</el-button>
      </div>
    </div>

    <div v-if="step === 0" class="libref-card">
      <h4>{{ messages.ingest.discoveryTitle }}</h4>
      <div class="libref-discovery-actions">
        <el-button size="small" :loading="busy" @click="discover('registry')">
          {{ messages.ingest.discoverRegistry }}
        </el-button>
        <el-button size="small" :loading="busy" @click="discover('web')">
          {{ messages.ingest.discoverWeb }}
        </el-button>
        <el-button size="small" :loading="busy" @click="discover('github')">
          {{ messages.ingest.discoverGithub }}
        </el-button>
      </div>
      <p v-if="discoverySource" class="libref-muted">{{ discoverySource }}</p>
      <EmptyState v-if="discovered && discovery.length === 0" :description="messages.ingest.discoveryEmpty" />
      <el-table v-else-if="discovery.length > 0" :data="discovery" size="small" stripe style="width: 100%">
        <el-table-column prop="name" :label="messages.common.name" />
        <el-table-column prop="title" :label="messages.catalog.descriptionCol" show-overflow-tooltip />
        <el-table-column prop="url" label="URL" show-overflow-tooltip />
        <el-table-column prop="version" :label="messages.common.version" width="140" />
        <el-table-column :label="messages.common.actions" width="90">
          <template #default="scope">
            <el-button link type="primary" size="small" @click="applyDiscovery(scope.row)">
              {{ messages.ingest.applyRow }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-if="step === 1" class="libref-card">
      <h4>{{ messages.ingest.step2 }}</h4>
      <p class="libref-muted">{{ messages.ingest.probeAddress(probeAddress) }}</p>
      <el-form @submit.prevent="probe">
        <el-form-item :label="messages.ingest.probeDocSamples">
          <el-input-number v-model="docSampleLimit" :min="1" :max="100" />
        </el-form-item>
        <el-form-item :label="messages.ingest.probeDocPages">
          <el-input-number v-model="docMaxPages" :min="1" />
        </el-form-item>
        <el-form-item :label="messages.ingest.probePrefilter">
          <el-checkbox v-model="docPrefilter">{{ messages.ingest.probePrefilterHint }}</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="busy" @click="probe">{{ messages.ingest.startProbe }}</el-button>
          <el-button @click="step = 0">{{ messages.common.previousStep }}</el-button>
        </el-form-item>
      </el-form>
      <div v-if="probeResult" class="libref-resolution">
        <RowTable :rows="[probeResult]" />
        <el-button type="primary" @click="step = 2">{{ messages.ingest.stepToSubmit }}</el-button>
      </div>
    </div>

    <div v-if="step === 2" class="libref-card">
      <h4>{{ messages.ingest.step3 }}</h4>
      <el-form @submit.prevent="submit">
        <el-form-item :label="messages.ingest.libraryName">
          <el-input v-model="form.name" style="width: 280px" />
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="submitVersion" :placeholder="messages.ingest.submitVersionPlaceholder" style="width: 200px" />
        </el-form-item>
        <el-form-item :label="messages.ingest.startUrl">
          <el-input v-model="submitUrl" style="width: 420px" />
        </el-form-item>
        <el-form-item :label="messages.ingest.maxPages">
          <el-input-number v-model="maxPages" :min="1" />
        </el-form-item>
        <el-form-item>
          <el-button @click="step = 1">{{ messages.common.previousStep }}</el-button>
          <el-button type="primary" :loading="busy" @click="submit">{{ messages.ingest.submitScrape }}</el-button>
        </el-form-item>
      </el-form>
    </div>

    <ErrorState v-if="error" :message="error" :request-id="requestId" @retry="resolve" />
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCheckbox, ElForm, ElFormItem, ElInput, ElInputNumber, ElStep, ElSteps, ElTable, ElTableColumn } from 'element-plus';
import { computed, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import PageHeader from '../components/common/PageHeader.vue';
import QuotaNotice from '../components/common/QuotaNotice.vue';
import { messages } from '../i18n';
import RowTable from '../components/common/RowTable.vue';
import { notifySuccess } from '../stores/notify';
import { invalidateCatalog } from '../stores/catalog';
import { scopeStore } from '../stores/scope';

type DiscoverySource = 'registry' | 'web' | 'github';

const router = useRouter();
const step = ref(0);
const busy = ref(false);
const error = ref('');
const requestId = ref('');

const form = reactive({ ecosystem: '', name: '', version: '' });
const resolution = ref<Row | null>(null);
const probeResult = ref<Row | null>(null);
const repoRedirect = ref(false);
const submitVersion = ref('');
const submitUrl = ref('');
const maxPages = ref<number | undefined>(undefined);
const docSampleLimit = ref<number | undefined>(undefined);
const docMaxPages = ref<number | undefined>(undefined);
const docPrefilter = ref(false);
const discovery = ref<Row[]>([]);
const discoverySource = ref('');
const discovered = ref(false);

const resolutionEntries = computed<KeyValueEntry[]>(() => {
  if (!resolution.value) return [];
  return Object.entries(resolution.value)
    .filter(([, value]) => typeof value !== 'object')
    .map(([label, value]) => ({ label, value: String(value ?? '') }));
});

const probeAddress = computed(() => {
  if (!resolution.value) return '';
  const value = resolution.value['start_url'] ?? resolution.value['url'];
  return typeof value === 'string' ? value : '';
});

// A resolved source address that is a code repository belongs on the request path,
// where approval is enforced. Detecting it here prevents a repo from being scraped as docs.
function isRepoAddress(value: string): boolean {
  if (!value) return false;
  if (/(^|\/)github\.com\//i.test(value) || /(^|\/)gitlab\.com\//i.test(value)) {
    const path = value.replace(/^https?:\/\//i, '').replace(/^[^/]+\//, '');
    return path.split('/').filter(Boolean).length >= 2;
  }
  return false;
}

// 429 is a quota refusal, not a failure: the submit path shows the reset time and
// a manual retry instead of an error panel.
const quotaReset = ref<string | null>(null);

function fail(err: unknown, title: string): void {
  if (err instanceof ApiError && err.status === 429) {
    quotaReset.value = err.resetAt || '';
    error.value = '';
    requestId.value = '';
    return;
  }
  error.value = err instanceof ApiError ? err.message : title;
  requestId.value = err instanceof ApiError ? err.requestId : '';
}

function retryQuota(): void {
  quotaReset.value = null;
  void submit();
}

async function discover(source: DiscoverySource): Promise<void> {
  const query = form.name.trim();
  if (!query) return;
  busy.value = true;
  error.value = '';
  discovered.value = false;
  try {
    const path =
      source === 'registry' ? '/library-search' : source === 'web' ? '/web-search' : '/github-search';
    const params =
      source === 'registry'
        ? { query, ecosystem: form.ecosystem || undefined, limit: 10, include_prerelease: scopeStore.includePrerelease }
        : { query, limit: 10 };
    const { data } = await api<Envelope<Row>>(path, { params });
    discovery.value = data.items;
    discoverySource.value =
      source === 'registry'
        ? messages.value.ingest.discoverRegistry
        : source === 'web'
          ? messages.value.ingest.discoverWeb
          : messages.value.ingest.discoverGithub;
    discovered.value = true;
  } catch (err) {
    fail(err, messages.value.ingest.discoverFailed);
  } finally {
    busy.value = false;
  }
}

function applyDiscovery(row: Row): void {
  const name = row['name'] ?? row['title'] ?? row['full_name'];
  const version = row['version'];
  const url = row['url'] ?? row['start_url'] ?? row['repo'] ?? row['repo_url'] ?? row['html_url'];
  if (typeof name === 'string' && name) form.name = name;
  if (typeof version === 'string' && version) form.version = version;
  if (typeof url === 'string' && url) submitUrl.value = url;
}

async function resolve(): Promise<void> {
  busy.value = true;
  error.value = '';
  resolution.value = null;
  repoRedirect.value = false;
  try {
    const { data } = await api<Row>('/resolve', {
      params: {
        ecosystem: form.ecosystem || undefined,
        name: form.name.trim(),
        version: form.version || undefined,
        target: 'docs',
        include_prerelease: scopeStore.includePrerelease,
      },
    });
    resolution.value = data;
    const address = data['start_url'] ?? data['url'] ?? data['repo'];
    const value = typeof address === 'string' ? address : '';
    if (isRepoAddress(value)) {
      repoRedirect.value = true;
    } else if (!submitUrl.value && value) {
      submitUrl.value = value;
    }
  } catch (err) {
    fail(err, messages.value.ingest.resolveFailed);
  } finally {
    busy.value = false;
  }
}

function goRequests(): void {
  void router.push({
    path: '/requests',
    query: {
      kind: 'repo',
      library: form.name.trim(),
      source: probeAddress.value || submitUrl.value || undefined,
      version: submitVersion.value || form.version || undefined,
      ecosystem: form.ecosystem || undefined,
    },
  });
}

async function probe(): Promise<void> {
  busy.value = true;
  error.value = '';
  probeResult.value = null;
  try {
    const { data } = await api<Row>('/doc-probe', {
      method: 'POST',
      body: {
        start_url: probeAddress.value || submitUrl.value,
        sample_limit: docSampleLimit.value,
        max_pages: docMaxPages.value,
        prefilter: docPrefilter.value,
      },
    });
    probeResult.value = data;
    if (!submitUrl.value && probeAddress.value) submitUrl.value = probeAddress.value;
  } catch (err) {
    fail(err, messages.value.ingest.probeFailed);
  } finally {
    busy.value = false;
  }
}

async function submit(): Promise<void> {
  if (!form.name.trim() || !submitVersion.value.trim() || !submitUrl.value.trim()) return;
  busy.value = true;
  error.value = '';
  try {
    const { data } = await api<{ id: number; ok: boolean }>('/scrape', {
      method: 'POST',
      body: {
        library: form.name.trim(),
        version: submitVersion.value.trim(),
        start_url: submitUrl.value.trim(),
        ecosystem: form.ecosystem || 'custom',
        max_pages: maxPages.value,
      },
    });
    invalidateCatalog();
    notifySuccess(messages.value.jobs.submitted(data.id));
    await router.push({ path: '/jobs', query: { id: String(data.id) } });
  } catch (err) {
    fail(err, messages.value.ingest.submitFailed);
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.libref-steps {
  margin-bottom: 16px;
}
.libref-resolution {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: flex-start;
}
.libref-discovery-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
</style>
