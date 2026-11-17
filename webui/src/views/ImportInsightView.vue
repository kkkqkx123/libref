<template>
  <div class="libref-page">
    <PageHeader title="导入" description="谁导入了它、该文件导入了谁" />
    <div class="libref-card">
      <el-form @submit.prevent="load">
        <el-form-item label="视图">
          <el-radio-group v-model="view">
            <el-radio-button label="importers">导入者</el-radio-button>
            <el-radio-button label="imports">被导入</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="API">
          <el-input v-model="apiName" clearable placeholder="模块或符号" style="width: 280px" />
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
      <EmptyState v-if="rows.length === 0" description="该条件下暂无导入边" />
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
import { scopeParams } from '../stores/scope';

type View = 'importers' | 'imports';
const view = ref<View>('importers');
const apiName = ref('');
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
    const { data } = await api<Envelope<Row>>('/import-insight', {
      params: {
        view: view.value,
        api: apiName.value.trim() || undefined,
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
