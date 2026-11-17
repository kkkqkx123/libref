<template>
  <div class="libref-page libref-login">
    <el-card class="libref-login-card" shadow="never">
      <h2>{{ messages.auth.loginTitle }}</h2>
      <p class="libref-muted">{{ messages.auth.loginDesc }}</p>
      <el-input
        v-model="key"
        type="password"
         :placeholder="messages.auth.keyPlaceholder"
        show-password
        @keyup.enter="submit"
      />
      <ErrorState v-if="error" :message="error" :request-id="requestId" @retry="submit" />
      <div class="libref-login-actions">
        <el-button type="primary" :loading="busy" :disabled="!key.trim()" @click="submit">
          {{ messages.common.login }}
        </el-button>
        <el-button link @click="enterDev">{{ messages.auth.devEnter }}</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ElButton, ElCard, ElInput } from 'element-plus';
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import { login } from '../stores/auth';
import { notifyError } from '../stores/notify';
import ErrorState from '../components/common/ErrorState.vue';
import { messages } from '../i18n';

const route = useRoute();
const router = useRouter();
const key = ref('');
const busy = ref(false);
const error = ref('');
const requestId = ref('');

async function submit(): Promise<void> {
  busy.value = true;
  error.value = '';
  requestId.value = '';
  try {
    await login(key.value);
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/search';
    await router.push(redirect);
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : messages.value.auth.loginFailed;
    requestId.value = err instanceof ApiError ? err.requestId : '';
    notifyError(err, messages.value.auth.loginFailed);
  } finally {
    busy.value = false;
  }
}

function enterDev(): void {
  void router.push('/search');
}
</script>

<style scoped>
.libref-login {
  display: flex;
  justify-content: center;
  padding-top: 80px;
}
.libref-login-card {
  width: 420px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.libref-login-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
