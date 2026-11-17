<template>
  <div class="libref-page">
    <PageHeader title="符号" description="符号详情、所在文件、子符号与签名" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item label="视图">
          <el-radio-group v-model="view">
            <el-radio-button label="detail">详情</el-radio-button>
            <el-radio-button label="file">所在文件</el-radio-button>
            <el-radio-button label="children">子符号</el-radio-button>
            <el-radio-button label="signature">签名</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="API">
          <el-input v-model="apiName" clearable placeholder="单个符号" style="width: 280px" />
        </el-form-item>
        <el-form-item label="多个 API">
          <TagInput v-model="apis" placeholder="逗号或回车分隔" />
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
      <EmptyState v-if="rows.length === 0" description="该条件下暂无符号" />
      <template v-else>
        <RowTable :rows="rows" />
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
import { ElButton, ElForm, ElFormItem, ElInput, ElRadioButton, ElRadioGroup } from 'element-plus';
import { ref } from 'vue';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import ScopeFilter from '../components/business/ScopeFilter.vue';
import EmptyState from '../components/common/EmptyState.vue';
import ErrorState from '../components/common/ErrorState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import RowTable from '../components/common/RowTable.vue';
import TagInput from '../components/common/TagInput.vue';
import { scopeParams } from '../stores/scope';

type View = 'detail' | 'file' | 'children' | 'signature';
const view = ref<View>('detail');
const apiName = ref('');
const apis = ref<string[]>([]);
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
    const { data } = await api<Envelope<Row>>('/symbol-insight', {
      params: {
        view: view.value,
        api: apiName.value.trim() || undefined,
        apis: apis.value.length > 0 ? apis.value.join(',') : undefined,
        ...scopeParams(),
        limit: limit.value,
        offset: offset.value,
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
</script>

<style scoped>
.libref-pager {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}
</style>
