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
        <el-table :data="keys" stripe class="libref-w-full">
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
        <el-table :data="principals" stripe class="libref-w-full">
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

      <el-tab-pane v-if="canConfig" :label="messages.admin.quotaTab" name="quota">
        <div class="libref-card">
          <div class="libref-quota-head">
            <h4>{{ messages.admin.quotaTab }}</h4>
            <el-button type="primary" size="small" @click="openQuota()">{{ messages.admin.quotaNew }}</el-button>
          </div>
          <LoadingPanel v-if="quotaLoading" :rows="2" />
          <EmptyState v-else-if="quotas.length === 0" :description="messages.admin.quotaEmpty" />
          <el-table v-else :data="quotas" stripe class="libref-w-full">
            <el-table-column prop="tenant_id" :label="messages.admin.quotaTenant" />
            <el-table-column prop="principal_id" :label="messages.admin.quotaPrincipal" width="100">
              <template #default="scope">{{ scope.row.principal_id ?? messages.common.none }}</template>
            </el-table-column>
            <el-table-column prop="resource" :label="messages.admin.quotaResource" />
            <el-table-column :label="messages.admin.quotaLimit" width="110">
              <template #default="scope">
                {{ scope.row.limit_value === 0 ? messages.admin.quotaUnlimited : scope.row.limit_value }}
              </template>
            </el-table-column>
            <el-table-column prop="window_type" :label="messages.admin.quotaWindow" width="100" />
            <el-table-column prop="updated_at" :label="messages.admin.quotaUpdatedAt" />
            <el-table-column :label="messages.common.actions" width="90">
              <template #default="scope">
                <el-button link type="primary" size="small" @click="openQuota(scope.row)">
                  {{ messages.common.edit }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
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

    <el-dialog v-model="quotaDialog" :title="quotaEditing ? messages.admin.quotaTab : messages.admin.quotaNew" width="480px">
      <el-form label-width="120px">
        <el-form-item :label="messages.admin.quotaResource">
          <el-select v-model="quotaForm.resource" filterable allow-create class="libref-w-full">
            <el-option v-for="r in quotaResources" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item :label="messages.admin.quotaLimit">
          <el-input-number v-model="quotaForm.limit_value" :min="0" class="libref-w-160"/>
          <span class="libref-muted">{{ messages.admin.quotaLimitHint }}</span>
        </el-form-item>
        <el-form-item :label="messages.admin.quotaWindow">
          <el-select v-model="quotaForm.window_type" class="libref-w-160">
            <el-option label="hour" value="hour" />
            <el-option label="day" value="day" />
            <el-option label="month" value="month" />
          </el-select>
        </el-form-item>
        <el-form-item :label="messages.admin.quotaTenant">
          <el-input v-model="quotaForm.tenant_id" :placeholder="messages.admin.quotaTenantHint" />
        </el-form-item>
        <el-form-item :label="messages.admin.quotaPrincipal">
          <el-input-number v-model="quotaForm.principal_id" :min="1" controls-position="right" />
          <span class="libref-muted">{{ messages.admin.quotaPrincipalHint }}</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="quotaDialog = false">{{ messages.common.cancel }}</el-button>
        <el-button type="primary" :loading="quotaSaving" @click="saveQuota">{{ messages.admin.quotaSave }}</el-button>
      </template>
    </el-dialog>

    <KeyIssueDialog v-model="issueOpen" @created="onKeyCreated" />
  </div>
</template>

<script setup lang="ts">
import {
  ElAlert, ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElInputNumber,
  ElOption, ElSelect, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag,
} from 'element-plus';
import { ElMessageBox } from 'element-plus';
import { computed, onUnmounted, reactive, ref, watch } from 'vue';
import { api, ApiError } from '../api/client';
import type {
  ApiKeyRow,
  MetricsResponse,
  PrincipalSummary,
  QuotaPolicy,
  ReadyResponse,
} from '../api/types';
import KeyIssueDialog from '../components/business/KeyIssueDialog.vue';
import EmptyState from '../components/common/EmptyState.vue';
import LoadingPanel from '../components/common/LoadingPanel.vue';
import PageHeader from '../components/common/PageHeader.vue';
import { messages } from '../i18n';
import { useFormatters } from '../i18n/format';
import KeyValue, { type KeyValueEntry } from '../components/common/KeyValue.vue';
import { hasScope } from '../stores/auth';
import { notifyError, notifySuccess } from '../stores/notify';

const format = useFormatters();
const canKeys = computed(() => hasScope('admin:keys'));
const canConfig = computed(() => hasScope('admin:config'));
const tab = ref(canKeys.value ? 'keys' : 'status');
let metricsTimer: number | null = null;

const keys = ref<ApiKeyRow[]>([]);
const principals = ref<PrincipalSummary[]>([]);
const conflictCount = computed(
  () => principals.value.filter((row) => row.conflicting.length > 0).length,
);
const newKey = ref('');
const issueOpen = ref(false);
const readyLoading = ref(false);
const metricsLoading = ref(false);
const metricsLoaded = ref(false);
const ready = ref<ReadyResponse | null>(null);
const metrics = ref<MetricsResponse | null>(null);

const QUOTA_RESOURCES = [
  'http_requests', 'llm_prompt_tokens', 'llm_completion_tokens', 'scrape_pages',
  'index_requests', 'job_submissions', 'snapshot_bytes', 'repos_bytes',
];
const quotaResources = QUOTA_RESOURCES;
const quotas = ref<QuotaPolicy[]>([]);
const quotaLoading = ref(false);
const quotaDialog = ref(false);
const quotaEditing = ref(false);
const quotaSaving = ref(false);
const quotaForm = reactive<{
  resource: string;
  limit_value: number;
  window_type: QuotaPolicy['window_type'];
  tenant_id: string;
  principal_id: number | undefined;
}>({ resource: 'http_requests', limit_value: 0, window_type: 'hour', tenant_id: '', principal_id: undefined });

const readyEntries = computed<KeyValueEntry[]>(() => {
  const value = ready.value;
  if (!value) return [];
  return Object.entries(value).map(([label, entry]) => ({
    label,
    value: typeof entry === 'object' ? JSON.stringify(entry) : String(entry ?? ''),
  }));
});

const metricEntries = computed<KeyValueEntry[]>(() => {
  const value = metrics.value;
  if (!value) return [];
  const scalars = Object.entries(value).filter(([label]) => label !== 'counters');
  const counters = Object.entries(value.counters || {});
  return [...scalars, ...counters].map(([label, entry]) => ({
    label,
    value: String(entry ?? ''),
  }));
});

async function loadKeys(): Promise<void> {
  if (!canKeys.value) return;
  try {
    const { data } = await api('/admin/keys', {});
    keys.value = data.items;
  } catch (err) {
    notifyError(err, messages.value.admin.loadKeysFailed);
  }
}

async function loadPrincipals(): Promise<void> {
  if (!canKeys.value) return;
  try {
    const { data } = await api('/admin/principals', {});
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
    await api('/admin/keys/{key_id}', { method: 'DELETE', pathParams: { key_id: id } });
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
    const { data } = await api('/admin/config/refresh', { method: 'POST' });
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
    const { data } = await api('/readyz', { auth: false });
    ready.value = data;
  } catch {
    ready.value = null;
  } finally {
    readyLoading.value = false;
  }
}

async function loadMetrics(): Promise<void> {
  if (!canConfig.value) return;
  metricsLoading.value = !metricsLoaded.value;
  try {
    const { data } = await api('/metrics', {});
    metrics.value = data;
  } catch {
    metrics.value = null;
  } finally {
    metricsLoaded.value = true;
    metricsLoading.value = false;
  }
}

async function loadQuotas(): Promise<void> {
  if (!canConfig.value) return;
  quotaLoading.value = true;
  try {
    const { data } = await api('/admin/quota/policies', {});
    quotas.value = data.items;
  } catch (err) {
    notifyError(err, messages.value.admin.quotaLoadFailed);
  } finally {
    quotaLoading.value = false;
  }
}

function openQuota(row?: unknown): void {
  const policy = row as QuotaPolicy | undefined;
  quotaEditing.value = Boolean(policy);
  quotaForm.resource = policy?.resource || 'http_requests';
  quotaForm.limit_value = policy?.limit_value || 0;
  quotaForm.window_type = policy?.window_type || 'hour';
  quotaForm.tenant_id = policy?.tenant_id || '';
  const principalId = policy ? Number(policy.principal_id) : NaN;
  quotaForm.principal_id = Number.isFinite(principalId) && principalId > 0 ? principalId : undefined;
  quotaDialog.value = true;
}

async function saveQuota(): Promise<void> {
  if (!quotaForm.resource) return;
  quotaSaving.value = true;
  try {
    await api('/admin/quota/policies', {
      method: 'PUT',
      body: {
        resource: quotaForm.resource,
        limit_value: quotaForm.limit_value,
        window_type: quotaForm.window_type,
        tenant_id: quotaForm.tenant_id.trim(),
        principal_id: quotaForm.principal_id ?? null,
      },
    });
    notifySuccess(messages.value.admin.quotaSaved);
    quotaDialog.value = false;
    await loadQuotas();
  } catch (err) {
    notifyError(err, messages.value.admin.quotaSaveFailed);
  } finally {
    quotaSaving.value = false;
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
    if (next === 'quota') {
      stopMetricsPolling();
      void loadQuotas();
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
  margin-top: var(--sp-3);
}
.libref-role-tag {
  margin-right: var(--sp-1);
}
.libref-separation-note {
  margin-bottom: var(--sp-3);
}
.libref-separation-ok {
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}
.libref-quota-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-3);
}
.libref-quota-head h4 {
  margin: 0;
}
</style>
