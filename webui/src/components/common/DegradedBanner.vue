<template>
  <el-alert
    v-if="visible"
    type="warning"
    show-icon
    :closable="false"
    class="libref-degraded"
  >
    <template #title>{{ messages.common.degradedTitle(answerabilityText) }}</template>
    <ul class="libref-note-list">
      <li v-for="reason in reasons" :key="reason">{{ reason }}</li>
    </ul>
  </el-alert>
</template>

<script setup lang="ts">
import { ElAlert } from 'element-plus';
import { computed } from 'vue';
import { messages } from '../../i18n';

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
  if (props.answerable === false) return messages.value.common.insufficientEvidence;
  const score = Number(props.answerability || 0);
  return messages.value.common.answerability(score);
});
</script>

<style scoped>
.libref-degraded {
  margin-bottom: var(--sp-4);
}
</style>
