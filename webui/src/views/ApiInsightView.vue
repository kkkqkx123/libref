<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.apiInsight" :description="messages.apiInsight.description" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item :label="messages.common.view">
          <el-radio-group v-model="view">
            <el-radio-button label="usage">{{ messages.apiInsight.usage }}</el-radio-button>
            <el-radio-button label="related">{{ messages.apiInsight.related }}</el-radio-button>
            <el-radio-button label="args">{{ messages.apiInsight.args }}</el-radio-button>
            <el-radio-button label="common">{{ messages.apiInsight.common }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="needsApi" label="API">
          <el-input v-model="apiName" clearable  :placeholder="messages.apiInsight.apiPlaceholder" style="width: 280px" />
        </el-form-item>
        <el-form-item v-if="view === 'common'" :label="messages.apiInsight.multipleApis">
          <TagInput v-model="apis"  :placeholder="messages.common.tagPlaceholder" />
        </el-form-item>
        <ScopeFilter />
        <el-form-item :label="messages.common.limit">
          <el-input-number v-model="limit" :min="1" :max="500" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="load">{{ messages.common.query }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <template v-else-if="loaded">
      <EmptyState v-if="rows.length === 0" :description="messages.common.noDataUnderCondition" />
      <EvidenceList v-else :rows="rows" @feedback="forwardFeedback" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup } from 'element-plus';
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EvidenceList from '../components/business/EvidenceList.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import TagInput from '../components/common/TagInput.vue';
import { notifyError } from '../stores/notify';
import { scopeParams } from '../stores/scope';

const router = useRouter();
type View = 'usage' | 'related' | 'args' | 'common';
const view = ref<View>('usage');
const apiName = ref('');
const apis = ref<string[]>([]);
const limit = ref(20);
const loading = ref(false);
const loaded = ref(false);
const error = ref('');
const requestId = ref('');
const rows = ref<Row[]>([]);

const needsApi = computed(() => view.value === 'related' || view.value === 'args');

async function load(): Promise<void> {
  if (needsApi.value && !apiName.value.trim()) {
    notifyError(new Error(messages.value.apiInsight.apiRequired), messages.value.common.missingParam);
    return;
  }
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<Row>>('/api-insight', {
      params: {
        view: view.value,
        api: apiName.value.trim() || undefined,
        apis: apis.value.length > 0 ? apis.value.join(',') : undefined,
        ...scopeParams(),
        limit: limit.value,
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

function forwardFeedback(row: Row): void {
  const callee = typeof row['callee_name'] === 'string' ? (row['callee_name'] as string) : '';
  void router.push({ path: '/feedback', query: { entity_type: 'call_instance', body: callee } });
}
</script>
