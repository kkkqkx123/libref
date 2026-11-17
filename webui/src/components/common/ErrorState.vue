<template>
  <el-result icon="error" :title="title" :sub-title="subtitle">
    <template #extra>
      <el-button type="primary" @click="emit('retry')">重试</el-button>
    </template>
  </el-result>
</template>

<script setup lang="ts">
import { ElButton, ElResult } from 'element-plus';
import { computed } from 'vue';

const props = defineProps<{ message: string; requestId?: string }>();
const emit = defineEmits<{ retry: [] }>();

const title = computed(() => props.message || '请求失败');
const subtitle = computed(() =>
  props.requestId ? `请求标识 ${props.requestId}` : '可将请求标识提供给管理员排查',
);
</script>
