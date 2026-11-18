// Typed fetch wrapper: injects the API key, normalizes FastAPI errors,
// and surfaces the backend request id for audit correlation.
import { authStore } from '../stores/auth';
import { messages } from '../i18n';

export class ApiError extends Error {
  status: number;
  // Stable backend code (``page_not_found``, ``quota_exceeded``, ...); falls back
  // to ``http_<status>`` when the cause is not mapped.
  code: string;
  // Raw backend detail, kept so a generic code can still name the culprit
  // (which argument, which upstream).
  detail: string;
  requestId: string;
  resetAt?: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    detail: string,
    requestId: string,
    resetAt?: string | null,
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
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

function statusFallback(status: number): string {
  const m = messages.value.common;
  if (status === 401) return m.authRequired;
  if (status === 403) return m.forbidden;
  if (status === 404) return m.notFound;
  if (status === 409) return m.conflict;
  if (status === 429) return m.quotaExceeded;
  return m.requestFailedStatus(status);
}

// Precedence: a known error code (localised) beats the raw detail text, which in
// turn beats generic status wording. A known code still carries its detail when
// the code is generic (``invalid_argument``), so the two are combined rather than
// letting the wording swallow the cause.
function describeFailure(
  status: number,
  payload: unknown,
): { code: string; message: string; detail: string } {
  let code = '';
  let detailText = '';
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    if (typeof record['error'] === 'string') code = record['error'];
    const detail = record['detail'];
    if (typeof detail === 'string') detailText = detail;
    else if (detail && typeof detail === 'object') {
      const inner = detail as Record<string, unknown>;
      if (typeof inner['error'] === 'string') detailText = inner['error'] as string;
    }
    if (!detailText && typeof record['message'] === 'string') detailText = record['message'];
  }
  if (!code) code = `http_${status}`;
  const localised = (messages.value.errorCodes as Record<string, string>)[code];
  if (localised) {
    const combined =
      detailText && detailText !== localised
        ? messages.value.common.errorWithDetail(localised, detailText)
        : localised;
    return { code, message: combined, detail: detailText };
  }
  if (detailText) return { code, message: detailText, detail: detailText };
  return { code, message: statusFallback(status), detail: '' };
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

export interface IndexRequestParams {
  limit?: number;
  status?: string;
  pending?: boolean;
}

export function listIndexRequests<T>(params: IndexRequestParams = {}) {
  const path = params.pending ? '/index-requests/pending' : '/index-requests';
  return api<T>(path, {
    params: {
      limit: params.limit,
      status: params.pending ? undefined : params.status,
    },
  });
}

export function submitIndexRequest<T>(body: unknown) {
  return api<T>('/index-requests', { method: 'POST', body });
}

export function actOnIndexRequest<T>(id: number, action: 'cancel' | 'approve' | 'reject', reason?: string) {
  return api<T>(`/index-requests/${id}/${action}`, {
    method: 'POST',
    body: reason === undefined ? undefined : { reason },
  });
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
    const failure = describeFailure(response.status, payload);
    throw new ApiError(
      response.status,
      failure.code,
      failure.message,
      failure.detail,
      requestId,
      resetOf(payload),
    );
  }
  return { data: payload as T, requestId };
}
