// Health state: liveness fingerprint plus readiness checks, polled while the
// application shell is mounted. Degradation never blocks read-only browsing.
import { reactive } from 'vue';
import { api } from '../api/client';

interface HealthState {
  fingerprint: string;
  degraded: boolean;
  failing: string[];
  loaded: boolean;
}

interface ReadyResponse {
  ok: boolean;
  checks?: Record<string, string>;
}

export const healthStore = reactive<HealthState>({
  fingerprint: '',
  degraded: false,
  failing: [],
  loaded: false,
});

let timer: number | null = null;

function failingChecks(checks: Record<string, string>): string[] {
  return Object.entries(checks)
    .filter(([, value]) => value !== 'ok' && value !== 'disabled')
    .map(([name, value]) => `${name}: ${value}`);
}

export async function refreshHealth(): Promise<void> {
  try {
    const { data } = await api<{ ok: boolean; fingerprint: string }>('/health', { auth: false });
    healthStore.fingerprint = data.fingerprint || '';
  } catch {
    healthStore.fingerprint = '';
  }
  try {
    const { data } = await api<ReadyResponse>('/readyz', { auth: false });
    healthStore.failing = failingChecks(data.checks || {});
    healthStore.degraded = !data.ok || healthStore.failing.length > 0;
  } catch {
    healthStore.failing = [];
    healthStore.degraded = false;
  } finally {
    healthStore.loaded = true;
  }
}

export function startHealthPolling(intervalMs = 30000): void {
  if (timer !== null) return;
  void refreshHealth();
  timer = window.setInterval(() => {
    void refreshHealth();
  }, intervalMs);
}

export function stopHealthPolling(): void {
  if (timer === null) return;
  window.clearInterval(timer);
  timer = null;
}
