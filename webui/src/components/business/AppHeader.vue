<template>
  <el-header class="libref-header">
    <div class="libref-header-brand" @click="goHome">libref</div>
    <div class="libref-header-right">
      <HealthDot :state="healthState" :label="healthLabel" />
      <el-dropdown @command="(cmd: string) => setLocale(cmd as 'zh-CN' | 'en')">
        <el-button link>{{ locale === 'zh-CN' ? '中文' : 'English' }}</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="zh-CN">中文</el-dropdown-item>
            <el-dropdown-item command="en">English</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button link @click="toggleTheme">
        {{ theme === 'light' ? messages.app.themeDark : messages.app.themeLight }}
      </el-button>
      <el-dropdown v-if="authStore.me" @command="onCommand">
        <span class="libref-header-user">{{ displayName() }}</span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="logout">{{ messages.common.logout }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button v-else-if="authStore.mode === 'enforced'" link @click="goLogin">{{ messages.common.login }}</el-button>
      <span v-else class="libref-muted">{{ messages.app.devMode }}</span>
    </div>
  </el-header>
</template>

<script setup lang="ts">
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElHeader } from 'element-plus';
import { messages } from '../../i18n';
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { api } from '../../api/client';
import { authStore, displayName, logout } from '../../stores/auth';
import { locale, setLocale } from '../../i18n';
import { theme, toggleTheme } from '../../stores/theme';
import HealthDot from '../common/HealthDot.vue';

const router = useRouter();
const fingerprint = ref('');

const healthState = computed(() => {
  if (!fingerprint.value) return 'unknown';
  return 'ok';
});
const healthLabel = computed(() =>
  fingerprint.value ? messages.value.header.serviceNormal(fingerprint.value) : messages.value.common.unknownStatus,
);

onMounted(() => {
  api<{ ok: boolean; fingerprint: string }>('/health', { auth: false })
    .then(({ data }) => {
      fingerprint.value = data.fingerprint || '';
    })
    .catch(() => {
      fingerprint.value = '';
    });
});

function goHome(): void {
  void router.push('/search');
}

function goLogin(): void {
  void router.push('/login');
}

function onCommand(command: string): void {
  if (command === 'logout') {
    logout();
    void router.push('/login');
  }
}
</script>

<style scoped>
.libref-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-light);
  background: var(--surface-card);
}
.libref-header-brand {
  font-weight: 700;
  font-size: 18px;
  cursor: pointer;
}
.libref-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.libref-header-user {
  cursor: pointer;
}
</style>
