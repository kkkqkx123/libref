<template>
  <div class="libref-evidence">
    <div
      v-for="(row, index) in rows"
      :key="index"
      class="libref-evidence-row"
    >
      <ConfidenceBadge v-if="confidenceOf(row)" :confidence="confidenceOf(row)" />
      <span class="libref-mono libref-ellipsis libref-evidence-main">{{ mainOf(row) }}</span>
      <span class="libref-muted libref-evidence-sub">{{ subOf(row) }}</span>
      <el-button
        v-if="inspectLabel"
        link
        type="primary"
        size="small"
        @click="emit('inspect', row)"
      >
        {{ inspectLabel }}
      </el-button>
      <el-button
        v-if="canFeedback"
        link
        type="primary"
        size="small"
        @click="emit('feedback', row)"
      >
        {{ messages.common.feedback }}
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ElButton } from 'element-plus';
import { computed } from 'vue';
import { messages } from '../../i18n';
import { field, type Row } from '../../api/types';
import { hasScope } from '../../stores/auth';
import ConfidenceBadge from '../common/ConfidenceBadge.vue';
import { formatCell } from '../../utils/format';

withDefaults(defineProps<{ rows: Row[]; inspectLabel?: string }>(), { inspectLabel: '' });
const emit = defineEmits<{ feedback: [row: Row]; inspect: [row: Row] }>();

const canFeedback = computed(() => hasScope('feedback:write'));

function pick(row: Row, keys: string[]): string {
  for (const key of keys) {
    const value = field(row, key);
    if (typeof value === 'string' && value) return value;
    if (typeof value === 'number') return String(value);
  }
  return '';
}

function confidenceOf(row: Row): string {
  return pick(row, ['confidence']);
}

function mainOf(row: Row): string {
  return (
    pick(row, ['callee', 'qualified_name', 'name', 'pattern']) ||
    formatCell(row).slice(0, 120)
  );
}

function subOf(row: Row): string {
  const file = pick(row, ['caller_file', 'file']);
  const start = pick(row, ['line_start']);
  const end = pick(row, ['line_end']);
  const range = start ? `:${start}${end ? `-${end}` : ''}` : '';
  const support = pick(row, ['support', 'count', 'frequency']);
  return [file ? `${file}${range}` : '', support ? messages.value.common.support(support) : '']
    .filter((part) => part)
    .join(' · ');
}
</script>

<style scoped>
.libref-evidence-row {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--border-light);
}
.libref-evidence-main {
  flex: 2;
}
.libref-evidence-sub {
  flex: 1;
  text-align: right;
}
</style>
