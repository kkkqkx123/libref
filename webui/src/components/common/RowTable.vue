<template>
  <el-table :data="rows" size="small" stripe class="libref-w-full">
    <el-table-column
      v-for="column in columns"
      :key="column"
      :prop="column"
      :label="column"
      show-overflow-tooltip
    >
      <template #default="scope">
        <span :class="{ 'libref-mono': isTechnical(value(scope.row, column)) }">
          {{ format(value(scope.row, column)) }}
        </span>
      </template>
    </el-table-column>
    <el-table-column v-if="inspectLabel" :label="messages.common.actions" width="110">
      <template #default="scope">
        <el-button link type="primary" size="small" @click="emit('inspect', scope.row)">
          {{ inspectLabel }}
        </el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup lang="ts">
import { ElButton, ElTable, ElTableColumn } from 'element-plus';
import { computed } from 'vue';
import { field, type JsonValue, type Row } from '../../api/types';
import { messages } from '../../i18n';
import { formatCell } from '../../utils/format';

const props = withDefaults(
  defineProps<{ rows: Row[]; maxColumns?: number; inspectLabel?: string }>(),
  { inspectLabel: '' },
);
const emit = defineEmits<{ inspect: [row: Row] }>();

const HIDDEN = new Set(['metadata']);

const columns = computed(() => {
  const names: string[] = [];
  for (const row of props.rows) {
    for (const key of Object.keys(row)) {
      if (!HIDDEN.has(key) && !names.includes(key)) names.push(key);
    }
    if (names.length >= (props.maxColumns || 8)) break;
  }
  return names;
});

function value(row: Row, key: string): JsonValue {
  return field(row, key);
}

function isTechnical(value: JsonValue): boolean {
  return typeof value === 'number' || (typeof value === 'string' && value.includes('/'));
}

function format(value: JsonValue): string {
  return formatCell(value);
}
</script>
