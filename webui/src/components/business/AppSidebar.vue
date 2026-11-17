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
import { messages } from '../../i18n';
import { hasScope, hasScopes } from '../../stores/auth';

interface NavItem {
  path: string;
  titleKey: keyof (typeof messages.value)['nav'];
  scopes: string[];
  any?: boolean;
}

interface NavGroupDef {
  nameKey: keyof (typeof messages.value)['nav'];
  single: boolean;
  items: NavItem[];
}

const GROUP_DEFS: NavGroupDef[] = [
  { nameKey: 'retrieval', single: false, items: [
    { path: '/catalog', titleKey: 'catalog', scopes: ['read'] },
    { path: '/search', titleKey: 'search', scopes: ['read'] },
    { path: '/answer', titleKey: 'answer', scopes: ['read'] },
  ]},
  { nameKey: 'insights', single: false, items: [
    { path: '/insights/api', titleKey: 'apiInsight', scopes: ['read'] },
    { path: '/insights/calls', titleKey: 'calls', scopes: ['read'] },
    { path: '/insights/symbols', titleKey: 'symbols', scopes: ['read'] },
    { path: '/insights/imports', titleKey: 'imports', scopes: ['read'] },
    { path: '/insights/patterns', titleKey: 'patterns', scopes: ['read'] },
  ]},
  { nameKey: 'ingestTasks', single: false, items: [
    { path: '/ingest', titleKey: 'ingest', scopes: ['ingest:write'] },
    { path: '/jobs', titleKey: 'jobs', scopes: ['read'] },
  ]},
  { nameKey: 'governance', single: false, items: [
    { path: '/review', titleKey: 'review', scopes: ['review:read'] },
    { path: '/feedback', titleKey: 'feedback', scopes: ['feedback:read', 'feedback:write'], any: true },
    { path: '/admin', titleKey: 'admin', scopes: ['admin:keys', 'admin:config'] },
  ]},
];

const route = useRoute();
const active = computed(() => route.path);

const visibleGroups = computed(() => {
  const m = messages.value;
  return GROUP_DEFS.map((group) => ({
    name: m.nav[group.nameKey],
    single: group.single,
    items: group.items
      .filter((item) =>
        item.any ? item.scopes.some((scope) => hasScope(scope)) : hasScopes(item.scopes),
      )
      .map((item) => ({ path: item.path, title: m.nav[item.titleKey], scopes: item.scopes, any: item.any })),
  })).filter((group) => group.items.length > 0);
});
</script>

<style scoped>
.libref-sidebar {
  border-right: 1px solid var(--border-light);
  background: var(--surface-card);
}
</style>
