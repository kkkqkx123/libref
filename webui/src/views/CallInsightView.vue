<template>
  <div class="libref-page">
    <PageHeader title="调用" description="按被调方、调用方、文件查看调用实例" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item label="视图">
          <el-radio-group v-model="view">
            <el-radio-button label="callee">被调方</el-radio-button>
            <el-radio-button label="caller">调用方</el-radio-button>
            <el-radio-button label="file">文件</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="API">
          <el-input v-model="apiName" clearable placeholder="限定被调或调用方" style="width: 280px" />
        </el-form-item>
        <el-form-item label="过滤">
          <el-checkbox v-model="includeAmbiguous">含模糊解析</el-checkbox>
        </el-form-item>
        <el-form-item label="解析种类">
          <TagInput v-model="resolveKinds" placeholder="如 EXTRACTED" />
        </el-form-item>
        <el-form-item label="实参数量">
          <el-input-number v-model="minArgs" :min="0" placeholder="最小" style="width: 110px" />
          <span class="libref-muted">—</span>
          <el-input-number v-model="maxArgs" :min="0" placeholder="最大" style="width: 110px" />
        </el-form-item>
        <ScopeFilter />
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="load">查询</el-button>
        </el-form-item>
      </el-form>
    </div>
    <LoadingPanel v-if="loading" />
    <ErrorState v-else-if="error" :message="error" :request-id="requestId" @retry="load" />
    <template v-else-if="loaded">
      <EmptyState v-if="rows.length === 0" description="该条件下暂无调用实例" />
      <template v-else>
        <EvidenceList :rows="rows" @feedback="forwardFeedback" />
        <div class="libref-pager">
          <el-button :disabled="offset === 0" @click="page(-1)">上一页</el-button>
          <span class="libref-muted">偏移 {{ offset }}</span>
          <el-button :disabled="rows.length < limit" @click="page(1)">下一页</el-button>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCheckbox, ElForm, ElFormItem, ElInput, ElInputNumber, ElRadioButton, ElRadioGroup } from 'element-plus';
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EvidenceList from '../components/business/EvidenceList.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import TagInput from '../components/common/TagInput.vue';
import { scopeParams } from '../stores/scope';

const router = useRouter();
type View = 'callee' | 'caller' | 'file';
const view = ref<View>('callee');
const apiName = ref('');
const includeAmbiguous = ref(false);
const resolveKinds = ref<string[]>([]);
const minArgs = ref<number | undefined>(undefined);
const maxArgs = ref<number | undefined>(undefined);
const limit = ref(50);
const offset = ref(0);
const loading = ref(false);
const loaded = ref(false);
const error = ref('');
const requestId = ref('');
const rows = ref<Row[]>([]);

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try {
    const { data } = await api<Envelope<Row>>('/call-insight', {
      params: {
        view: view.value,
        api: apiName.value.trim() || undefined,
        ...scopeParams(),
        limit: limit.value,
        offset: offset.value,
        include_ambiguous: includeAmbiguous.value,
        resolve_kinds: resolveKinds.value.length > 0 ? resolveKinds.value.join(',') : undefined,
        min_args: minArgs.value,
        max_args: maxArgs.value,
      },
    });
    rows.value = data.items;
    loaded.value = true;
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '查询失败';
    requestId.value = err instanceof ApiError ? err.requestId : '';
  } finally {
    loading.value = false;
  }
}

function page(direction: number): void {
  offset.value = Math.max(0, offset.value + direction * limit.value);
  void load();
}

function forwardFeedback(row: Row): void {
  void router.push({
    path: '/feedback',
    query: { entity_type: 'call_instance', body: String(row['entity_id'] || '') },
  });
}
</script>

<style scoped>
.libref-pager {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}
</style>
