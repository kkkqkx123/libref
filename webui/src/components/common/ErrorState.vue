<template>
  <el-result icon="error" :title="title" :sub-title="subtitle">
    <template #extra>
      <el-button type="primary" @click="emit('retry')">{{ messages.common.retry }}</el-button>
    </template>
  </el-result>
</template>

<script setup lang="ts">
import { ElButton, ElResult } from 'element-plus';
import { computed } from 'vue';
import { messages } from '../../i18n';

const props = defineProps<{ message: string; requestId?: string }>();
const emit = defineEmits<{ retry: [] }>();

const title = computed(() => props.message || messages.value.common.requestFailed);
const subtitle = computed(() =>
  props.requestId ? messages.value.common.requestId(props.requestId) : messages.value.common.requestIdHint,
);
</script>
