<template>
  <el-table :data="rows" size="small" stripe class="libref-w-full">
    <el-table-column
      v-for="column in columns"
      :key="column"
      :prop="column"
      :label="labelOf(column)"
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

<script setup lang="ts" generic="T extends object">
import { ElButton, ElTable, ElTableColumn } from 'element-plus';
import type { ColumnKey } from '../../api/types';
import { field, type JsonValue, type Row } from '../../api/types';
import { messages } from '../../i18n';
import { formatCell } from '../../utils/format';

// Columns are declared by the caller: the table renders exactly the fields the
// view consumes, and a column name that the row type does not carry fails
// type-checking instead of silently showing an empty column.
const props = withDefaults(
  defineProps<{
    rows: T[];
    columns: readonly ColumnKey<T>[];
    labels?: Partial<Record<ColumnKey<T>, string>>;
    inspectLabel?: string;
  }>(),
  { inspectLabel: '', labels: undefined },
);
const emit = defineEmits<{ inspect: [row: T] }>();

function labelOf(column: ColumnKey<T>): string {
  return props.labels?.[column] ?? column;
}

function value(row: T, key: ColumnKey<T>): JsonValue {
  return field(row as unknown as Row, key);
}

function isTechnical(value: JsonValue): boolean {
  return typeof value === 'number' || (typeof value === 'string' && value.includes('/'));
}

function format(value: JsonValue): string {
  return formatCell(value);
}
</script>
