<template>
  <el-alert type="warning" show-icon :closable="false" class="libref-quota">
    <template #title>{{ messages.common.quotaTitle }}</template>
    <p class="libref-quota-hint">{{ hint }}</p>
    <slot />
  </el-alert>
</template>

<script setup lang="ts">
// Shared 429 notice: resource and reset time, plus a slot for the page's own way
// out. No countdown — the reset time is shown in local time and retrying stays
// manual, because a ticking clock would only guess at the server's schedule.
import { ElAlert } from 'element-plus';
import { computed } from 'vue';
import { messages } from '../../i18n';
import { useFormatters } from '../../i18n/format';

const props = defineProps<{ resetAt?: string | null }>();

const { dateTime } = useFormatters();

const hint = computed(() =>
  props.resetAt
    ? messages.value.common.quotaResetHint(dateTime(props.resetAt))
    : messages.value.common.quotaExceeded,
);
</script>

<style scoped>
.libref-quota {
  margin-bottom: 16px;
}
.libref-quota-hint {
  margin: 4px 0 8px;
}
</style>
