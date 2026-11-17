<template>
  <div class="libref-page libref-login">
    <el-card class="libref-login-card" shadow="never">
      <h2>登录 libref</h2>
      <p class="libref-muted">enforced 模式使用 API 密钥登录；开发模式免登录直接进入。</p>
      <el-input
        v-model="key"
        type="password"
        placeholder="粘贴 API 密钥"
        show-password
        @keyup.enter="submit"
      />
      <ErrorState v-if="error" :message="error" :request-id="requestId" @retry="submit" />
      <div class="libref-login-actions">
        <el-button type="primary" :loading="busy" :disabled="!key.trim()" @click="submit">
          登录
        </el-button>
        <el-button link @click="enterDev">以开发模式进入</el-button>
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
    error.value = err instanceof ApiError ? err.message : '登录失败';
    requestId.value = err instanceof ApiError ? err.requestId : '';
    notifyError(err, '登录失败');
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
