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
import { NAV_ROUTES, type NavGroupKey } from '../../router/nav';
import { hasScope, hasScopes } from '../../stores/auth';

// Group order and presentation only; membership and permissions come from the
// shared route table so the sidebar cannot disagree with the router guard.
const GROUPS: { group: NavGroupKey; single: boolean }[] = [
  { group: 'retrieval', single: false },
  { group: 'insights', single: false },
  { group: 'ingestTasks', single: false },
  { group: 'governance', single: false },
];

const route = useRoute();
const active = computed(() => route.path);

const visibleGroups = computed(() => {
  const m = messages.value;
  return GROUPS.map((entry) => ({
    name: m.nav[entry.group],
    single: entry.single,
    items: NAV_ROUTES
      .filter((item) => item.group === entry.group)
      .filter((item) =>
        item.any ? item.scopes.some((scope) => hasScope(scope)) : hasScopes(item.scopes),
      )
      .map((item) => ({ path: item.route.path, title: m.nav[item.titleKey] })),
  })).filter((group) => group.items.length > 0);
});
</script>

<style scoped>
.libref-sidebar {
  border-right: 1px solid var(--border-light);
  background: var(--surface-card);
}
</style>
