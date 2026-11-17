<template>
  <div class="libref-page">
    <EmptyState description="当前主体缺少访问该页面所需的权限">
      <el-button type="primary" @click="back">返回搜索</el-button>
    </EmptyState>
  </div>
</template>

<script setup lang="ts">
import { ElButton } from 'element-plus';
import { useRouter } from 'vue-router';
import EmptyState from '../components/common/EmptyState.vue';

const router = useRouter();
function back(): void {
  void router.push('/search');
}
</script>
