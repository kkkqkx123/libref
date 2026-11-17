<template>
  <el-empty :description="description || messages.common.noData">
    <slot />
  </el-empty>
</template>

<script setup lang="ts">
import { ElEmpty } from 'element-plus';

import { messages } from '../../i18n';

withDefaults(defineProps<{ description?: string }>(), {});
</script>
