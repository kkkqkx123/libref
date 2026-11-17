<template>
  <el-table :data="rows" size="small" stripe style="width: 100%">
    <el-table-column
      v-for="column in columns"
      :key="column"
      :prop="column"
      :label="column"
      show-overflow-tooltip
    >
      <template #default="scope">
        <span :class="{ 'libref-mono': isTechnical(scope.row[column]) }">
          {{ format(scope.row[column]) }}
        </span>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup lang="ts">
import { ElTable, ElTableColumn } from 'element-plus';
import { computed } from 'vue';
import type { Row } from '../../api/types';
import { formatCell } from '../../utils/format';

const props = defineProps<{ rows: Row[]; maxColumns?: number }>();

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

function isTechnical(value: unknown): boolean {
  return typeof value === 'number' || (typeof value === 'string' && value.includes('/'));
}

function format(value: unknown): string {
  return formatCell(value);
}
</script>
