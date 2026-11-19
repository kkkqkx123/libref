// Typed fetch wrapper: paths, query strings, request bodies and responses come
// from the checked-in OpenAPI schema. It also injects the API key, normalizes
// FastAPI errors, and surfaces the backend request id for audit correlation.
import type { ApiErrorCode, KnownErrorCode } from './types';
import type { components, paths } from './schema';
import { authStore } from '../stores/auth';
import { messages } from '../i18n';

export class ApiError extends Error {
  status: number;
  // Stable backend code (`page_not_found`, `quota_exceeded`, ...); falls back
  // to `http_<status>` when the cause is not mapped.
  code: ApiErrorCode;
  // Raw backend detail, kept so a generic code can still name the culprit
  // (which argument, which upstream).
  detail: string;
  requestId: string;
  resetAt?: string | null;

  constructor(
    status: number,
    code: ApiErrorCode,
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

export type ApiPath = keyof paths & string;
export type ApiMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

type Operation<P extends ApiPath, M extends ApiMethod> = Extract<
  paths[P][Lowercase<M> & keyof paths[P]],
  { responses: unknown }
>;

type QueryOf<P extends ApiPath, M extends ApiMethod> =
  Operation<P, M>['parameters'] extends { query?: infer Q }
    ? Q extends never
      ? never
      : NonNullable<Q>
    : never;

type PathParamsOf<P extends ApiPath, M extends ApiMethod> =
  Operation<P, M>['parameters'] extends { path?: infer T }
    ? T extends never
      ? never
      : NonNullable<T>
    : never;

type BodyOf<P extends ApiPath, M extends ApiMethod> =
  Operation<P, M>['requestBody'] extends {
    content: { 'application/json': infer B };
  }
    ? B
    : never;

type MethodsOf<P extends ApiPath> = {
  [M in ApiMethod]: [Operation<P, M>] extends [never] ? never : M;
}[ApiMethod];

type RequiredKeys<T> = {
  [K in keyof T]-?: {} extends Pick<T, K> ? never : K;
}[keyof T];

type ResponseContent<T> = T extends {
  content: { 'application/json': infer B };
}
  ? B
  : never;

export type ResponseOf<
  P extends ApiPath,
  M extends ApiMethod = 'GET',
> = ResponseContent<Operation<P, M>['responses'][200]>;

type MethodOption<P extends ApiPath, M extends ApiMethod> = [M] extends [
  MethodsOf<P>
]
  ? { method?: M }
  : never;

type QueryOption<P extends ApiPath, M extends ApiMethod> = [QueryOf<P, M>] extends [
  never
]
  ? { params?: never }
  : [RequiredKeys<QueryOf<P, M>>] extends [never]
    ? { params?: QueryOf<P, M> }
    : { params: QueryOf<P, M> };

type BodyOption<P extends ApiPath, M extends ApiMethod> = [BodyOf<P, M>] extends [
  never
]
  ? { body?: never }
  : { body: BodyOf<P, M> };

type PathOption<P extends ApiPath, M extends ApiMethod> = [
  PathParamsOf<P, M>
] extends [never]
  ? { pathParams?: never }
  : { pathParams: PathParamsOf<P, M> };

export type ApiOptions<P extends ApiPath, M extends ApiMethod> = MethodOption<P, M> &
  QueryOption<P, M> &
  BodyOption<P, M> &
  PathOption<P, M> & {
    auth?: boolean;
  };

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
// the code is generic (`invalid_argument`), so the two are combined rather than
// letting the wording swallow the cause.
type ErrorBody = components['schemas']['ErrorResponse'];

function errorBodyOf(payload: unknown): ErrorBody | null {
  if (!payload || typeof payload !== 'object') return null;
  const body = payload as ErrorBody;
  return typeof body.error === 'string' ? body : null;
}

function describeFailure(
  status: number,
  payload: unknown,
): { code: ApiErrorCode; message: string; detail: string } {
  const body = errorBodyOf(payload);
  let code: ApiErrorCode = `http_${status}`;
  let detailText = '';
  if (body?.error) {
    code = body.error;
    if (typeof body.detail === 'string') detailText = body.detail;
    else if (body.detail) detailText = body.detail.error;
  }
  const localised = (messages.value.errorCodes as Record<KnownErrorCode, string>)[
    code as KnownErrorCode
  ];
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
  const body = errorBodyOf(payload);
  if (!body) return null;
  if (typeof body.reset_at === 'string') return body.reset_at;
  if (typeof body.detail !== 'string' && typeof body.detail?.reset_at === 'string') {
    return body.detail.reset_at;
  }
  return null;
}

function renderPath<P extends ApiPath, M extends ApiMethod>(
  path: P,
  values: PathParamsOf<P, M> | undefined,
): string {
  const record = values as Record<string, string | number> | undefined;
  return path.replace(/\{([^}]+)\}/g, (_match, key: string) => {
    const value = record?.[key];
    if (value === undefined) throw new Error(`missing path parameter: ${key}`);
    return encodeURIComponent(String(value));
  });
}

export async function api<P extends ApiPath, M extends ApiMethod = 'GET'>(
  path: P,
  options: ApiOptions<P, M>,
): Promise<{ data: ResponseOf<P, M>; requestId: string }> {
  const query = new URLSearchParams();
  const params = options.params as
    | Record<string, string | string[] | number | boolean | null | undefined>
    | undefined;
  for (const [key, value] of Object.entries(params || {})) {
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== '') query.append(key, String(item));
      }
      continue;
    }
    if (value === undefined || value === null || value === '') continue;
    query.append(key, String(value));
  }
  const rendered = renderPath(path, options.pathParams);
  const suffix = query.toString();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (options.auth !== false) {
    const key = authStore.key;
    if (key) headers['Authorization'] = `Bearer ${key}`;
  }
  const response = await fetch(`${baseUrl()}${rendered}${suffix ? `?${suffix}` : ''}`, {
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
  return { data: payload as ResponseOf<P, M>, requestId };
}
