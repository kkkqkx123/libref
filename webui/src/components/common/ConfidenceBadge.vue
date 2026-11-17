<template>
  <el-tag :type="tagType" size="small" effect="plain">{{ confidence }}</el-tag>
</template>

<script setup lang="ts">
import { ElTag } from 'element-plus';
import { computed } from 'vue';

const props = defineProps<{ confidence: string }>();

const tagType = computed(() => {
  switch ((props.confidence || '').toUpperCase()) {
    case 'EXTRACTED':
      return 'success';
    case 'INFERRED':
      return 'primary';
    case 'AMBIGUOUS':
      return 'warning';
    default:
      return 'info';
  }
});
</script>
