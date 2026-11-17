// Notification helpers: user-facing messages always carry the request id
// so failures can be correlated with the backend audit log.
import { ElMessage, ElNotification } from 'element-plus';
import { ApiError } from '../api/client';
import { messages } from '../i18n';

export function notifyError(error: unknown, title = messages.value.common.requestFailed): void {
  if (error instanceof ApiError) {
    const suffix = error.requestId ? `（${messages.value.common.requestId(error.requestId)}）` : '';
    const quota = error.resetAt ? messages.value.common.quotaReset(error.resetAt) : '';
    ElNotification.error({ title, message: `${error.message}${quota}${suffix}` });
    return;
  }
  ElNotification.error({ title, message: error instanceof Error ? error.message : String(error) });
}

export function notifySuccess(message: string): void {
  ElMessage.success(message);
}
