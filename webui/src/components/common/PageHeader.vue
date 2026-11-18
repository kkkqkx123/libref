<template>
  <div class="libref-page-header">
    <div>
      <h2 class="libref-page-title">{{ title }}</h2>
      <p v-if="description" class="libref-muted">{{ description }}</p>
    </div>
    <div class="libref-page-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ title: string; description?: string }>();
</script>

<style scoped>
.libref-page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-4);
  margin-bottom: var(--sp-4);
}
.libref-page-title {
  margin: 0 0 var(--sp-1);
  font-size: var(--fs-xl);
}
.libref-page-actions {
  display: flex;
  gap: var(--sp-2);
  flex-shrink: 0;
}
</style>
