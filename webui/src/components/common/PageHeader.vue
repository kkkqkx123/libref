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
  gap: 16px;
  margin-bottom: 16px;
}
.libref-page-title {
  margin: 0 0 4px;
  font-size: 20px;
}
.libref-page-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
</style>
