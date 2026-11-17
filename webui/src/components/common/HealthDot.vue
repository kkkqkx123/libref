<template>
  <span class="libref-health" :class="tone">
    <span class="libref-health-dot" />
    {{ label }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{ state: 'ok' | 'degraded' | 'unknown'; label: string }>();

const tone = computed(() => `libref-health-${props.state}`);
</script>

<style scoped>
.libref-health {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.libref-health-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-placeholder);
}
.libref-health-ok .libref-health-dot {
  background: var(--semantic-success);
}
.libref-health-degraded .libref-health-dot {
  background: var(--semantic-warning);
}
</style>
