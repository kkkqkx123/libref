<template>
  <div class="libref-page">
    <PageHeader title="管理" description="密钥、配置、指标与健康状态">
      <template #actions>
        <el-button v-if="canKeys" type="primary" @click="issueOpen = true">签发密钥</el-button>
        <el-button v-if="canConfig" @click="refreshConfig">刷新配置</el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="tab">
      <el-tab-pane v-if="canKeys" label="密钥" name="keys">
        <el-table :data="keys" stripe style="width: 100%">
          <el-table-column prop="id" label="标识" width="80" />
          <el-table-column prop="principal_id" label="主体" width="90" />
          <el-table-column prop="key_prefix" label="前缀" width="140" />
          <el-table-column prop="scopes" label="权限域" show-overflow-tooltip />
          <el-table-column prop="expires_at" label="过期" width="170" />
          <el-table-column prop="revoked_at" label="吊销" width="170">
            <template #default="scope">{{ scope.row.revoked_at ? '已吊销' : '有效' }}</template>
          </el-table-column>
          <el-table-column prop="last_used_at" label="最后使用" width="170" />
          <el-table-column label="操作" width="100">
            <template #default="scope">
              <el-button
                v-if="!scope.row.revoked_at"
                link
                type="danger"
                size="small"
                @click="revoke(scope.row.id)"
              >
                吊销
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-alert
          v-if="newKey"
          type="success"
          :closable="false"
          class="libref-new-key"
        >
          <template #title>新密钥原文（仅展示一次）</template>
          <span class="libref-mono">{{ newKey }}</span>
          <el-button link type="primary" size="small" @click="copyKey">复制</el-button>
        </el-alert>
      </el-tab-pane>

      <el-tab-pane v-if="canConfig" label="状态" name="status">
        <div class="libref-card">
          <h4>就绪检查</h4>
          <LoadingPanel v-if="readyLoading" :rows="2" />
          <KeyValue v-else :entries="readyEntries" />
        </div>
        <div class="libref-card">
          <h4>进程计数</h4>
          <LoadingPanel v-if="metricsLoading" :rows="2" />
          <KeyValue v-else :entries="metricEntries" />
        </div>
      </el-tab-pane>
    </el-tabs>

    <KeyIssueDialog v-model="issueOpen" @created="onKeyCreated" />
  </div>
</template>

<script setup lang="ts">
import { ElAlert, ElButton, ElTabPane, ElTable, ElTableColumn, ElTabs } from 'element-plus';
import { ElMessageBox } from 'element-plus';
import { computed, ref } from 'vue';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import KeyIssueDialog from '../components/business/KeyIssueDialog.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';

const canKeys = hasScope('admin:keys');
const canConfig = hasScope('admin:config');
const tab = ref(canKeys ? 'keys' : 'status');

const keys = ref<Row[]>([]);
const newKey = ref('');
const issueOpen = ref(false);
const readyLoading = ref(false);
const metricsLoading = ref(false);
const ready = ref<Record<string, unknown>>({});
const metrics = ref<Record<string, unknown>>({});

const readyEntries = computed<KeyValueEntry[]>(() =>
  Object.entries(ready.value).map(([label, value]) => ({
    label,
    value: typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''),
  })),
);

const metricEntries = computed<KeyValueEntry[]>(() =>
  Object.entries(metrics.value).map(([label, value]) => ({
    label,
    value: typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''),
  })),
);

async function loadKeys(): Promise<void> {
  if (!canKeys) return;
  try {
    const { data } = await api<Envelope<Row>>('/admin/keys');
    keys.value = data.items;
  } catch (err) {
    notifyError(err, '加载密钥失败');
  }
}

async function revoke(id: number): Promise<void> {
  try {
    await ElMessageBox.confirm('确定吊销该密钥吗？吊销后立即失效。', '吊销密钥', { type: 'warning' });
  } catch {
    return;
  }
  try {
    await api<{ ok: boolean }>(`/admin/keys/${id}`, { method: 'DELETE' });
    notifySuccess('已吊销');
    await loadKeys();
  } catch (err) {
    notifyError(err, '吊销失败');
  }
}

function onKeyCreated(key: string): void {
  newKey.value = key;
  void loadKeys();
}

async function copyKey(): Promise<void> {
  try {
    await navigator.clipboard.writeText(newKey.value);
    notifySuccess('已复制');
  } catch {
    notifyError(new Error('复制失败，请手动选择复制'), '复制失败');
  }
}

async function refreshConfig(): Promise<void> {
  try {
    const { data } = await api<{ ok: boolean; old_fingerprint: string; fingerprint: string }>(
      '/admin/config/refresh',
      { method: 'POST' },
    );
    if (data.old_fingerprint === data.fingerprint) notifySuccess('配置无变化');
    else notifySuccess(`配置已刷新：${data.fingerprint.slice(0, 12)}`);
  } catch (err) {
    if (err instanceof ApiError) notifyError(err, '刷新失败');
    else notifyError(err, '刷新失败');
  }
}

async function loadStatus(): Promise<void> {
  if (!canConfig) return;
  readyLoading.value = true;
  metricsLoading.value = true;
  try {
    const { data } = await api<Row>('/readyz', { auth: false });
    ready.value = data;
  } catch {
    ready.value = {};
  } finally {
    readyLoading.value = false;
  }
  try {
    const { data } = await api<Row>('/metrics');
    metrics.value = data;
  } catch {
    metrics.value = {};
  } finally {
    metricsLoading.value = false;
  }
}

void loadKeys();
void loadStatus();
</script>

<style scoped>
.libref-new-key {
  margin-top: 12px;
}
</style>
