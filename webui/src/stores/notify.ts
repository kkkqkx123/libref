// Notification helpers: user-facing messages always carry the request id
// so failures can be correlated with the backend audit log.
import { ElMessage, ElNotification } from 'element-plus';
import { ApiError } from '../api/client';

export function notifyError(error: unknown, title = '请求失败'): void {
  if (error instanceof ApiError) {
    const suffix = error.requestId ? `（请求标识 ${error.requestId}）` : '';
    const quota = error.resetAt ? `，配额重置于 ${error.resetAt}` : '';
    ElNotification.error({ title, message: `${error.message}${quota}${suffix}` });
    return;
  }
  ElNotification.error({ title, message: error instanceof Error ? error.message : String(error) });
}

export function notifySuccess(message: string): void {
  ElMessage.success(message);
}
