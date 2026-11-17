<template>
  <el-empty :description="description">
    <slot />
  </el-empty>
</template>

<script setup lang="ts">
import { ElEmpty } from 'element-plus';

withDefaults(defineProps<{ description?: string }>(), { description: '暂无数据' });
</script>
