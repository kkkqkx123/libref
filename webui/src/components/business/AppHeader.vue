<template>
  <el-header class="libref-header">
    <div class="libref-header-brand" @click="goHome">libref</div>
    <div class="libref-header-right">
      <HealthDot :state="healthState" :label="healthLabel" />
      <el-dropdown @command="(cmd: string) => setLocale(cmd as 'zh-CN' | 'en')">
        <el-button link>{{ locale === 'zh-CN' ? messages.language.zh : messages.language.en }}</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="zh-CN">{{ messages.language.zh }}</el-dropdown-item>
            <el-dropdown-item command="en">{{ messages.language.en }}</el-dropdown-item>
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
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { authStore, displayName, logout } from '../../stores/auth';
import { locale, setLocale } from '../../i18n';
import { healthStore } from '../../stores/health';
import { theme, toggleTheme } from '../../stores/theme';
import HealthDot from '../common/HealthDot.vue';

const router = useRouter();

const healthState = computed<'ok' | 'degraded' | 'unknown'>(() => {
  if (!healthStore.loaded) return 'unknown';
  return healthStore.degraded ? 'degraded' : 'ok';
});
const healthLabel = computed(() => {
  if (!healthStore.loaded) return messages.value.common.unknownStatus;
  if (healthStore.degraded) {
    const names = healthStore.failing.map((entry) => entry.split(':')[0].trim());
    return messages.value.header.serviceDegraded(names.join(', '));
  }
  return healthStore.fingerprint
    ? messages.value.header.serviceNormal(healthStore.fingerprint)
    : messages.value.common.unknownStatus;
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
