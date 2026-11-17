<template>
  <el-card class="libref-citation" shadow="never">
    <div class="libref-citation-title libref-ellipsis">{{ citation.title || citation.url }}</div>
    <div class="libref-muted libref-mono libref-ellipsis">{{ citation.url }}</div>
    <div class="libref-citation-meta">
      <el-tag v-if="citation.library" size="small">{{ citation.library }}</el-tag>
      <el-tag v-if="citation.version" size="small" type="info">{{ citation.version }}</el-tag>
      <el-button link type="primary" size="small" @click="open">{{ messages.common.viewSource }}</el-button>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ElButton, ElCard, ElTag } from 'element-plus';
import { useRouter } from 'vue-router';
import { messages } from '../../i18n';
import type { Citation } from '../../api/types';

const props = defineProps<{ citation: Citation }>();
const router = useRouter();

function open(): void {
  void router.push({
    path: '/reader',
    query: {
      url: props.citation.url,
      library: props.citation.library || undefined,
      version: props.citation.version || undefined,
    },
  });
}
</script>

<style scoped>
.libref-citation {
  margin-bottom: 8px;
}
.libref-citation-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.libref-citation-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 8px;
}
</style>
