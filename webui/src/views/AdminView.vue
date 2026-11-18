<template>
  <div class="libref-page">
    <PageHeader :title="messages.routes.admin" :description="messages.admin.description">
      <template #actions>
        <el-button v-if="canKeys" type="primary" @click="issueOpen = true">{{ messages.admin.issueKey }}</el-button>
        <el-button v-if="canConfig" @click="refreshConfig">{{ messages.admin.refreshConfig }}</el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="tab">
      <el-tab-pane v-if="canKeys"  :label="messages.admin.keysTab" name="keys">
        <el-table :data="keys" stripe style="width: 100%">
          <el-table-column prop="id" :label="messages.jobs.id" width="80" />
          <el-table-column prop="principal_id" :label="messages.admin.principal" width="90" />
          <el-table-column prop="key_prefix" :label="messages.admin.prefix" width="140" />
          <el-table-column prop="scopes" :label="messages.admin.scopes" show-overflow-tooltip />
          <el-table-column :label="messages.admin.expiresAt" width="170">
            <template #default="scope">{{ format.dateTime(scope.row.expires_at) }}</template>
          </el-table-column>
          <el-table-column :label="messages.admin.revokedAt" width="170">
            <template #default="scope">{{ scope.row.revoked_at ? format.dateTime(scope.row.revoked_at) : messages.admin.valid }}</template>
          </el-table-column>
          <el-table-column :label="messages.admin.lastUsed" width="170">
            <template #default="scope">{{ format.dateTime(scope.row.last_used_at) }}</template>
          </el-table-column>
          <el-table-column  :label="messages.common.actions" width="100">
            <template #default="scope">
              <el-button
                v-if="!scope.row.revoked_at"
                link
                type="danger"
                size="small"
                @click="revoke(scope.row.id)"
              >
                {{ messages.common.revoke }}
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
          <template #title>{{ messages.admin.newKeyTitle }}</template>
          <span class="libref-mono">{{ newKey }}</span>
          <el-button link type="primary" size="small" @click="copyKey">{{ messages.common.copy }}</el-button>
        </el-alert>
      </el-tab-pane>

      <el-tab-pane v-if="canKeys" :label="messages.admin.principalsTab" name="principals">
        <el-alert
          v-if="conflictCount > 0"
          type="warning"
          :closable="false"
          class="libref-separation-note"
        >
          <template #title>{{ messages.admin.separationHint(conflictCount) }}</template>
        </el-alert>
        <el-table :data="principals" stripe style="width: 100%">
          <el-table-column prop="id" :label="messages.jobs.id" width="70" />
          <el-table-column prop="tenant_id" :label="messages.admin.tenant" width="110" />
          <el-table-column prop="name" :label="messages.admin.principal" width="170" />
          <el-table-column prop="kind" :label="messages.admin.kind" width="90" />
          <el-table-column :label="messages.admin.roles" width="220">
            <template #default="scope">
              <el-tag
                v-for="role in scope.row.roles"
                :key="role"
                :type="roleSeverity(scope.row.conflicting, role)"
                size="small"
                class="libref-role-tag"
              >
                {{ role }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="scopes" :label="messages.admin.scopes" show-overflow-tooltip>
            <template #default="scope">{{ scope.row.scopes.join(', ') }}</template>
          </el-table-column>
          <el-table-column :label="messages.admin.separation" width="180">
            <template #default="scope">
              <el-tag v-if="scope.row.conflicting.length > 0" type="danger" size="small">
                {{ scope.row.conflicting.map(groupLabel).join(' | ') }}
              </el-tag>
              <span v-else class="libref-separation-ok">{{ messages.admin.separationOk }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canConfig"  :label="messages.common.status" name="status">
        <div class="libref-card">
          <h4>{{ messages.admin.readiness }}</h4>
          <LoadingPanel v-if="readyLoading" :rows="2" />
          <KeyValue v-else :entries="readyEntries" />
        </div>
        <div class="libref-card">
          <h4>{{ messages.admin.metrics }}</h4>
          <LoadingPanel v-if="metricsLoading" :rows="2" />
          <KeyValue v-else :entries="metricEntries" />
        </div>
      </el-tab-pane>
    </el-tabs>

    <KeyIssueDialog v-model="issueOpen" @created="onKeyCreated" />
  </div>
</template>

<script setup lang="ts">
import { ElAlert, ElButton, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag } from 'element-plus';
import { ElMessageBox } from 'element-plus';
import { computed, onUnmounted, ref, watch } from 'vue';
import { api, ApiError } from '../api/client';
import type { Envelope, Row } from '../api/types';
import KeyIssueDialog from '../components/business/KeyIssueDialog.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import { useFormatters } from '../i18n/format';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';

interface PrincipalRow {
  id: number;
  tenant_id: string;
  kind: string;
  name: string;
  status: string;
  roles: string[];
  scopes: string[];
  conflicting: string[][];
}

const format = useFormatters();
const canKeys = computed(() => hasScope('admin:keys'));
const canConfig = computed(() => hasScope('admin:config'));
const tab = ref(canKeys.value ? 'keys' : 'status');
let metricsTimer: number | null = null;

const keys = ref<Row[]>([]);
const principals = ref<PrincipalRow[]>([]);
const conflictCount = computed(
  () => principals.value.filter((row) => row.conflicting.length > 0).length,
);
const newKey = ref('');
const issueOpen = ref(false);
const readyLoading = ref(false);
const metricsLoading = ref(false);
const metricsLoaded = ref(false);
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
  if (!canKeys.value) return;
  try {
    const { data } = await api<Envelope<Row>>('/admin/keys');
    keys.value = data.items;
  } catch (err) {
    notifyError(err, messages.value.admin.loadKeysFailed);
  }
}

async function loadPrincipals(): Promise<void> {
  if (!canKeys.value) return;
  try {
    const { data } = await api<Envelope<PrincipalRow>>('/admin/principals');
    principals.value = data.items;
  } catch (err) {
    notifyError(err, messages.value.admin.loadPrincipalsFailed);
  }
}

function roleSeverity(conflicting: string[][], role: string): 'danger' | 'info' {
  return conflicting.some((group) => group.includes(role)) ? 'danger' : 'info';
}

function groupLabel(group: string[]): string {
  return group.join(' + ');
}

async function revoke(id: number): Promise<void> {
  try {
    await ElMessageBox.confirm(messages.value.admin.revokeConfirm, messages.value.admin.revokeKey, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await api<{ ok: boolean }>(`/admin/keys/${id}`, { method: 'DELETE' });
    notifySuccess(messages.value.admin.revokedDone);
    await loadKeys();
  } catch (err) {
    notifyError(err, messages.value.admin.revokeFailed);
  }
}

function onKeyCreated(key: string): void {
  newKey.value = key;
  void loadKeys();
}

async function copyKey(): Promise<void> {
  try {
    await navigator.clipboard.writeText(newKey.value);
    notifySuccess(messages.value.common.copied);
  } catch {
    notifyError(new Error(messages.value.common.copyFailedManual), messages.value.common.copyFailed);
  }
}

async function refreshConfig(): Promise<void> {
  try {
    const { data } = await api<{ ok: boolean; old_fingerprint: string; fingerprint: string }>(
      '/admin/config/refresh',
      { method: 'POST' },
    );
    if (data.old_fingerprint === data.fingerprint) notifySuccess(messages.value.admin.configUnchanged);
    else notifySuccess(messages.value.admin.configRefreshed(data.fingerprint.slice(0, 12)));
  } catch (err) {
    if (err instanceof ApiError) notifyError(err, messages.value.admin.refreshFailed);
    else notifyError(err, messages.value.admin.refreshFailed);
  }
}

async function loadReadiness(): Promise<void> {
  if (!canConfig.value) return;
  readyLoading.value = true;
  try {
    const { data } = await api<Row>('/readyz', { auth: false });
    ready.value = data;
  } catch {
    ready.value = {};
  } finally {
    readyLoading.value = false;
  }
}

async function loadMetrics(): Promise<void> {
  if (!canConfig.value) return;
  metricsLoading.value = !metricsLoaded.value;
  try {
    const { data } = await api<Row>('/metrics');
    metrics.value = data;
  } catch {
    metrics.value = {};
  } finally {
    metricsLoaded.value = true;
    metricsLoading.value = false;
  }
}

// Process counters keep moving; polling runs only while the status tab is open.
function startMetricsPolling(): void {
  if (metricsTimer !== null) return;
  metricsTimer = window.setInterval(() => {
    void loadMetrics();
  }, 10000);
}

function stopMetricsPolling(): void {
  if (metricsTimer === null) return;
  window.clearInterval(metricsTimer);
  metricsTimer = null;
}

watch(
  tab,
  (next) => {
    if (next === 'principals') {
      stopMetricsPolling();
      void loadPrincipals();
      return;
    }
    if (next !== 'status') {
      stopMetricsPolling();
      return;
    }
    void loadReadiness();
    void loadMetrics();
    startMetricsPolling();
  },
  { immediate: true },
);

onUnmounted(() => stopMetricsPolling());

void loadKeys();
</script>

<style scoped>
.libref-new-key {
  margin-top: 12px;
}
.libref-role-tag {
  margin-right: 4px;
}
.libref-separation-note {
  margin-bottom: 12px;
}
.libref-separation-ok {
  color: var(--text-secondary);
  font-size: 12px;
}
</style>
