<template>
  <div class="libref-tag-input">
    <el-tag
      v-for="(item, index) in modelValue"
      :key="`${item}-${index}`"
      closable
      class="libref-tag-input-tag"
      @close="remove(index)"
    >
      {{ item }}
    </el-tag>
    <el-input
      v-model="draft"
      :placeholder="placeholder"
      class="libref-tag-input-box"
      @keyup.enter="commit"
      @blur="commit"
    />
  </div>
</template>

<script setup lang="ts">
import { ElInput, ElTag } from 'element-plus';
import { ref } from 'vue';

withDefaults(defineProps<{ placeholder?: string }>(), { placeholder: '输入后回车添加' });
const modelValue = defineModel<string[]>({ default: () => [] });
const draft = ref('');

function commit(): void {
  const parts = draft.value
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  if (parts.length > 0) {
    modelValue.value = [...modelValue.value, ...parts];
  }
  draft.value = '';
}

function remove(index: number): void {
  modelValue.value = modelValue.value.filter((_, i) => i !== index);
}
</script>

<style scoped>
.libref-tag-input {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.libref-tag-input-box {
  min-width: 180px;
  flex: 1;
}
</style>
