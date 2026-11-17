<template>
  <el-container class="libref-root">
    <AppHeader />
    <el-container>
      <AppSidebar v-if="showSidebar" />
      <el-main class="libref-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { ElContainer, ElMain } from 'element-plus';
import { computed } from 'vue';
import { useRoute } from 'vue-router';
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
