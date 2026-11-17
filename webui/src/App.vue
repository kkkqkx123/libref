<template>
  <el-config-provider :locale="elementLocale">
    <el-container class="libref-root">
      <AppHeader />
      <el-container>
        <AppSidebar v-if="showSidebar" />
        <el-main class="libref-main">
          <router-view />
        </el-main>
      </el-container>
    </el-container>
  </el-config-provider>
</template>

<script setup lang="ts">
import { ElConfigProvider, ElContainer, ElMain } from 'element-plus';
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { elementLocale } from './i18n/element';
import AppHeader from './components/business/AppHeader.vue';
import AppSidebar from './components/business/AppSidebar.vue';

const route = useRoute();
const showSidebar = computed(() => route.path !== '/login');
</script>

<style>
html,
body,
#app {
  margin: 0;
  height: 100%;
}
body {
  background: var(--surface-bg);
  color: var(--text-primary);
}
</style>

<style scoped>
.libref-root {
  height: 100vh;
  flex-direction: column;
}
.libref-main {
  padding: 0;
  overflow-y: auto;
}
</style>
