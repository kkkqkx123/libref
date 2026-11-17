<template>
  <div class="libref-page">
    <PageHeader title="入库向导" description="解析定位、探测评估、正式提交三步完成入库" />
    <el-steps :active="step" finish-status="success" class="libref-steps">
      <el-step title="解析定位" />
      <el-step title="探测评估" />
      <el-step title="正式提交" />
    </el-steps>

    <div v-if="step === 0" class="libref-card">
      <h4>第一步：解析定位</h4>
      <el-form @submit.prevent="resolve">
        <el-form-item label="生态">
          <el-input v-model="form.ecosystem" placeholder="如 npm、pypi" style="width: 200px" />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="包名" style="width: 280px" />
          <el-button link type="primary" @click="quickSearch">用注册表联想填充</el-button>
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="form.version" placeholder="留空取最新" style="width: 200px" />
        </el-form-item>
        <el-form-item label="目标">
          <el-radio-group v-model="form.target">
            <el-radio-button label="docs">文档</el-radio-button>
            <el-radio-button label="repo">仓库</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="busy" :disabled="!form.name.trim()" @click="resolve">
            解析
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="resolution" class="libref-resolution">
        <KeyValue :entries="resolutionEntries" />
        <el-button type="primary" @click="step = 1">下一步：探测</el-button>
      </div>
    </div>

    <div v-if="step === 1" class="libref-card">
      <h4>第二步：探测评估</h4>
      <p class="libref-muted">地址：{{ probeAddress }}</p>
      <el-button type="primary" :loading="busy" @click="probe">开始探测</el-button>
      <el-button @click="step = 0">上一步</el-button>
      <div v-if="probeResult" class="libref-resolution">
        <RowTable :rows="[probeResult]" />
        <el-button type="primary" @click="step = 2">下一步：提交</el-button>
      </div>
    </div>

    <div v-if="step === 2" class="libref-card">
      <h4>第三步：正式提交</h4>
      <el-form @submit.prevent="submit">
        <el-form-item label="库名">
          <el-input v-model="form.name" style="width: 280px" />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="submitVersion" placeholder="提交的版本号" style="width: 200px" />
        </el-form-item>
        <el-form-item label="起始地址">
          <el-input v-model="submitUrl" style="width: 420px" />
        </el-form-item>
        <el-form-item label="页数上限">
          <el-input-number v-model="maxPages" :min="1" />
        </el-form-item>
        <el-form-item>
          <el-button @click="step = 1">上一步</el-button>
          <el-button type="primary" :loading="busy" @click="submit">提交采集任务</el-button>
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
    fail(err, '联想失败');
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
    fail(err, '解析失败');
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
    fail(err, '探测失败');
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
    notifySuccess(`任务已提交：${data.id}`);
    await router.push({ path: '/jobs', query: { id: String(data.id) } });
  } catch (err) {
    fail(err, '提交失败');
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
