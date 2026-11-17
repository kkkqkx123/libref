<template>
  <div class="libref-results">
    <el-card
      v-for="item in items"
      :key="item.id"
      shadow="never"
      class="libref-result-card"
    >
      <div class="libref-result-title">
        <span class="libref-ellipsis">{{ item.title || item.url }}</span>
        <el-tag size="small" type="info">{{ scoreText(item.score) }}</el-tag>
      </div>
      <div class="libref-muted libref-mono libref-ellipsis">{{ item.url }}</div>
      <p class="libref-result-snippet">{{ item.snippet }}</p>
      <div class="libref-result-meta">
        <el-tag v-if="item.library" size="small">{{ item.library }}</el-tag>
        <el-tag v-if="item.version" size="small" type="info">{{ item.version }}</el-tag>
        <span class="libref-muted">{{ item.path }}</span>
        <span class="libref-result-actions">
          <el-button link type="primary" size="small" @click="emit('open-read', item)">
            阅读
          </el-button>
          <el-button link type="primary" size="small" @click="emit('open-answer', item)">
            问答
          </el-button>
        </span>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCard, ElTag } from 'element-plus';
import type { SearchItem } from '../../api/types';

defineProps<{ items: SearchItem[] }>();
const emit = defineEmits<{
  'open-read': [item: SearchItem];
  'open-answer': [item: SearchItem];
}>();

function scoreText(score: number): string {
  return `得分 ${Number(score || 0).toFixed(2)}`;
}
</script>

<style scoped>
.libref-result-card {
  margin-bottom: 12px;
}
.libref-result-title {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-weight: 600;
  margin-bottom: 4px;
}
.libref-result-snippet {
  margin: 8px 0;
  font-size: 14px;
  line-height: 1.6;
}
.libref-result-meta {
  display: flex;
  gap: 8px;
  align-items: center;
}
.libref-result-actions {
  margin-left: auto;
}
</style>
