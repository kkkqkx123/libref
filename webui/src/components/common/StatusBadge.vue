<template>
  <el-tag :type="tagType" size="small">{{ label }}</el-tag>
</template>

<script setup lang="ts">
import { ElTag } from 'element-plus';
import { computed } from 'vue';
import { messages } from '../../i18n';
import type { MessageCatalog } from '../../i18n/locales/zh-CN';

const props = defineProps<{ status: string }>();

const GOOD = new Set(['done', 'approved', 'resolved', 'acknowledged']);
const BAD = new Set(['failed', 'conflict']);
const WARN = new Set(['running', 'pending', 'open']);

const tagType = computed(() => {
  const status = props.status.toLowerCase();
  if (GOOD.has(status)) return 'success';
  if (BAD.has(status)) return 'danger';
  if (WARN.has(status)) return 'warning';
  return 'info';
});

const label = computed(() => {
  const keys = messages.value.status as MessageCatalog['status'];
  const key = (props.status || 'unknown').toLowerCase() as keyof MessageCatalog['status'];
  return keys[key] || props.status || messages.value.status.unknown;
});
</script>
