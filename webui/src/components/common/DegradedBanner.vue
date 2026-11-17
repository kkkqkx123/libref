<template>
  <el-alert
    v-if="visible"
    type="warning"
    show-icon
    :closable="false"
    class="libref-degraded"
  >
    <template #title>检索已降级：{{ answerabilityText }}</template>
    <ul class="libref-degraded-list">
      <li v-for="reason in reasons" :key="reason">{{ reason }}</li>
    </ul>
  </el-alert>
</template>

<script setup lang="ts">
import { ElAlert } from 'element-plus';
import { computed } from 'vue';

const props = defineProps<{
  degraded: boolean;
  reasons: string[];
  answerable?: boolean;
  answerability?: number;
}>();

const visible = computed(
  () => props.degraded || props.answerable === false || props.reasons.length > 0,
);

const answerabilityText = computed(() => {
  if (props.answerable === false) return '当前证据不足以回答';
  const score = Number(props.answerability || 0);
  return `可回答度 ${score.toFixed(2)}`;
});
</script>

<style scoped>
.libref-degraded {
  margin-bottom: 16px;
}
.libref-degraded-list {
  margin: 4px 0 0;
  padding-left: 18px;
}
</style>
