<template>
  <el-aside width="200px" class="libref-sidebar">
    <el-menu :default-active="active" router>
      <template v-for="group in visibleGroups" :key="group.name">
        <el-menu-item
          v-if="group.single"
          :index="group.items[0].path"
        >
          {{ group.items[0].title }}
        </el-menu-item>
        <el-sub-menu v-else :index="group.name">
          <template #title>{{ group.name }}</template>
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
            {{ item.title }}
          </el-menu-item>
        </el-sub-menu>
      </template>
    </el-menu>
  </el-aside>
</template>

<script setup lang="ts">
import { ElAside, ElMenu, ElMenuItem, ElSubMenu } from 'element-plus';
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { hasScope, hasScopes } from '../../stores/auth';

interface NavItem {
  path: string;
  title: string;
  scopes: string[];
  any?: boolean;
}

interface NavGroup {
  name: string;
  single: boolean;
  items: NavItem[];
}

const GROUPS: NavGroup[] = [
  { name: '检索问答', single: false, items: [
    { path: '/catalog', title: '文库目录', scopes: ['read'] },
    { path: '/search', title: '搜索', scopes: ['read'] },
    { path: '/answer', title: '问答', scopes: ['read'] },
  ]},
  { name: '源码洞察', single: false, items: [
    { path: '/insights/api', title: 'API 洞察', scopes: ['read'] },
    { path: '/insights/calls', title: '调用', scopes: ['read'] },
    { path: '/insights/symbols', title: '符号', scopes: ['read'] },
    { path: '/insights/imports', title: '导入', scopes: ['read'] },
    { path: '/insights/patterns', title: '模式', scopes: ['read'] },
  ]},
  { name: '入库任务', single: false, items: [
    { path: '/ingest', title: '入库向导', scopes: ['ingest:write'] },
    { path: '/jobs', title: '任务中心', scopes: ['read'] },
  ]},
  { name: '治理', single: false, items: [
    { path: '/review', title: '审核', scopes: ['review:read'] },
    { path: '/feedback', title: '反馈', scopes: ['feedback:read', 'feedback:write'], any: true },
    { path: '/admin', title: '管理', scopes: ['admin:keys', 'admin:config'] },
  ]},
];

const route = useRoute();
const active = computed(() => route.path);

const visibleGroups = computed(() =>
  GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) =>
      item.any ? item.scopes.some((scope) => hasScope(scope)) : hasScopes(item.scopes),
    ),
  })).filter((group) => group.items.length > 0),
);
</script>

<style scoped>
.libref-sidebar {
  border-right: 1px solid var(--border-light);
  background: var(--surface-card);
}
</style>
