// Typed fetch wrapper: injects the API key, normalizes FastAPI errors,
// and surfaces the backend request id for audit correlation.
import { authStore } from '../stores/auth';
import { messages } from '../i18n';

export class ApiError extends Error {
  status: number;
  requestId: string;
  resetAt?: string | null;

  constructor(status: number, message: string, requestId: string, resetAt?: string | null) {
    super(message);
    this.status = status;
    this.requestId = requestId;
    this.resetAt = resetAt;
  }
}

export interface ApiOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | boolean | null | undefined>;
  auth?: boolean;
}

function baseUrl(): string {
  return (import.meta.env.VITE_API_BASE as string | undefined) || '';
}

function describeFailure(status: number, payload: unknown): string {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    const detail = record['detail'];
    if (typeof detail === 'string' && detail) return detail;
    if (detail && typeof detail === 'object') {
      const inner = detail as Record<string, unknown>;
      if (typeof inner['error'] === 'string') return inner['error'] as string;
    }
    if (typeof record['message'] === 'string') return record['message'] as string;
    if (typeof record['error'] === 'string') return record['error'] as string;
  }
  const m = messages.value.common;
  if (status === 401) return m.authRequired;
  if (status === 403) return m.forbidden;
  if (status === 404) return m.notFound;
  if (status === 409) return m.conflict;
  if (status === 429) return m.quotaExceeded;
  return m.requestFailedStatus(status);
}

function resetOf(payload: unknown): string | null {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    const detail = record['detail'];
    if (detail && typeof detail === 'object') {
      const reset = (detail as Record<string, unknown>)['reset_at'];
      if (typeof reset === 'string') return reset;
    }
    const reset = record['reset_at'];
    if (typeof reset === 'string') return reset;
  }
  return null;
}

export async function api<T>(path: string, options: ApiOptions = {}): Promise<{ data: T; requestId: string }> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(options.params || {})) {
    if (value === undefined || value === null || value === '') continue;
    params.append(key, String(value));
  }
  const query = params.toString();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (options.auth !== false) {
    const key = authStore.key;
    if (key) headers['Authorization'] = `Bearer ${key}`;
  }
  const response = await fetch(`${baseUrl()}${path}${query ? `?${query}` : ''}`, {
    method: options.method || 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const requestId =
    response.headers.get('x-request-id') || `local-${Date.now().toString(36)}`;
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, describeFailure(response.status, payload), requestId, resetOf(payload));
  }
  return { data: payload as T, requestId };
}
