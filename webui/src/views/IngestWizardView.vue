<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.ingest" :description="messages.ingest.description" />
    <el-steps :active="step" finish-status="success" class="libref-steps">
      <el-step :title="messages.ingest.stepResolve" />
      <el-step :title="messages.ingest.stepProbe" />
      <el-step :title="messages.ingest.stepSubmit" />
    </el-steps>

    <div v-if="step === 0" class="libref-card">
      <h4>{{ messages.ingest.step1 }}</h4>
      <el-form @submit.prevent="resolve">
        <el-form-item :label="messages.common.ecosystem">
          <el-input v-model="form.ecosystem"  :placeholder="messages.ingest.ecosystemPlaceholder" style="width: 200px" />
        </el-form-item>
        <el-form-item :label="messages.common.name">
          <el-input v-model="form.name"  :placeholder="messages.ingest.namePlaceholder" style="width: 280px" />
          <el-button link type="primary" @click="quickSearch">{{ messages.ingest.quickSearch }}</el-button>
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="form.version"  :placeholder="messages.ingest.versionPlaceholder" style="width: 200px" />
        </el-form-item>
        <el-form-item :label="messages.ingest.target">
          <el-radio-group v-model="form.target">
            <el-radio-button label="docs">{{ messages.common.docs }}</el-radio-button>
            <el-radio-button label="repo">{{ messages.common.repo }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="busy" :disabled="!form.name.trim()" @click="resolve">
            {{ messages.common.parse }}
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="resolution" class="libref-resolution">
        <KeyValue :entries="resolutionEntries" />
        <el-button type="primary" @click="step = 1">{{ messages.common.nextStep }}：{{ messages.ingest.probe }}</el-button>
      </div>
    </div>

    <div v-if="step === 1" class="libref-card">
      <h4>{{ messages.ingest.step2 }}</h4>
      <p class="libref-muted">{{ messages.ingest.address }}：{{ probeAddress }}</p>
      <el-button type="primary" :loading="busy" @click="probe">{{ messages.ingest.startProbe }}</el-button>
      <el-button @click="step = 0">{{ messages.common.previousStep }}</el-button>
      <div v-if="probeResult" class="libref-resolution">
        <RowTable :rows="[probeResult]" />
        <el-button type="primary" @click="step = 2">{{ messages.common.nextStep }}：{{ messages.common.submit }}</el-button>
      </div>
    </div>

    <div v-if="step === 2" class="libref-card">
      <h4>{{ messages.ingest.step3 }}</h4>
      <el-form @submit.prevent="submit">
        <el-form-item :label="messages.ingest.libraryName">
          <el-input v-model="form.name" style="width: 280px" />
        </el-form-item>
        <el-form-item :label="messages.common.version">
          <el-input v-model="submitVersion"  :placeholder="messages.ingest.submitVersionPlaceholder" style="width: 200px" />
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
import { ElButton, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup, ElStep, ElSteps } from 'element-plus';
import { computed, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Row } from '../api/types';
import ErrorState from '../components/common/ErrorState.vue';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import RowTable from '../components/common/RowTable.vue';
import { notifySuccess } from '../stores/notify';
import { invalidateCatalog } from '../stores/catalog';

const router = useRouter();
const step = ref(0);
const busy = ref(false);
const error = ref('');
const requestId = ref('');

const form = reactive({ ecosystem: '', name: '', version: '', target: 'docs' });
const resolution = ref<Row | null>(null);
const probeResult = ref<Row | null>(null);
const submitVersion = ref('');
const submitUrl = ref('');
const maxPages = ref<number | undefined>(undefined);

const resolutionEntries = computed<KeyValueEntry[]>(() => {
  if (!resolution.value) return [];
  return Object.entries(resolution.value)
    .filter(([, value]) => typeof value !== 'object')
    .map(([label, value]) => ({ label, value: String(value ?? '') }));
});

const probeAddress = computed(() => {
  if (!resolution.value) return '';
  const value = resolution.value['start_url'] ?? resolution.value['url'] ?? resolution.value['repo'];
  return typeof value === 'string' ? value : '';
});

function fail(err: unknown, title: string): void {
  error.value = err instanceof ApiError ? err.message : title;
  requestId.value = err instanceof ApiError ? err.requestId : '';
}

async function quickSearch(): Promise<void> {
  if (!form.name.trim()) return;
  busy.value = true;
  error.value = '';
  try {
    const { data } = await api<{ items: Row[] }>('/library-search', {
      params: { query: form.name.trim(), ecosystem: form.ecosystem || undefined, limit: 5 },
    });
    const first = data.items[0];
    if (first && typeof first['name'] === 'string') form.name = first['name'] as string;
  } catch (err) {
    fail(err, messages.value.ingest.suggestFailed);
  } finally {
    busy.value = false;
  }
}

async function resolve(): Promise<void> {
  busy.value = true;
  error.value = '';
  resolution.value = null;
  try {
    const { data } = await api<Row>('/resolve', {
      params: {
        ecosystem: form.ecosystem || undefined,
        name: form.name.trim(),
        version: form.version || undefined,
        target: form.target,
      },
    });
    resolution.value = data;
  } catch (err) {
    fail(err, messages.value.ingest.resolveFailed);
  } finally {
    busy.value = false;
  }
}

async function probe(): Promise<void> {
  busy.value = true;
  error.value = '';
  probeResult.value = null;
  try {
    if (form.target === 'docs') {
      const { data } = await api<Row>('/doc-probe', {
        method: 'POST',
        body: { start_url: probeAddress.value || submitUrl.value },
      });
      probeResult.value = data;
      if (!submitUrl.value && probeAddress.value) submitUrl.value = probeAddress.value;
    } else {
      const { data } = await api<Row>('/repo-probe', {
        method: 'POST',
        body: { source: probeAddress.value || form.name.trim() },
      });
      probeResult.value = data;
    }
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
</style>
