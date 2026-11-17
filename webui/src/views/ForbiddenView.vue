<template>
  <div class="libref-page">
    <EmptyState  :description="messages.common.forbiddenPage">
      <el-button type="primary" @click="back">{{ messages.common.backToSearch }}</el-button>
    </EmptyState>
  </div>
</template>

<script setup lang="ts">
import { ElButton } from 'element-plus';
import { useRouter } from 'vue-router';
import EmptyState from '../components/common/EmptyState.vue';
import { messages } from '../i18n';

const router = useRouter();
function back(): void {
  void router.push('/search');
}
</script>
