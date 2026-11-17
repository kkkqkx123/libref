<template>
  <el-descriptions :column="1" size="small" border>
    <el-descriptions-item
      v-for="entry in entries"
      :key="entry.label"
      :label="entry.label"
    >
      {{ entry.value }}
    </el-descriptions-item>
  </el-descriptions>
</template>

<script setup lang="ts">
import { ElDescriptions, ElDescriptionsItem } from 'element-plus';

export interface KeyValueEntry {
  label: string;
  value: string;
}

defineProps<{ entries: KeyValueEntry[] }>();
</script>
