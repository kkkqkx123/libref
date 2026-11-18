// Global library/version/ecosystem filter shared by search, answer, and insights.
import { reactive, watch } from 'vue';

const STORAGE_KEY = 'libref.scope_filter';

export type SearchPreset = 'precise' | 'balanced' | 'thorough';

interface ScopeState {
  library: string;
  version: string;
  ecosystem: string;
  preset: SearchPreset;
  // Retrieval material budget for search/answer (small, preset-driven).
  limit: number;
  // Result-page size shared by the five read-only insight lists. Distinct from
  // limit because those pages cap at 500 while the retrieval budget is small.
  resultLimit: number;
  // Whether resolution and search may surface pre-release library versions.
  includePrerelease: boolean;
}

export const RESULT_LIMIT_MIN = 1;
export const RESULT_LIMIT_MAX = 500;

function initial(): ScopeState {
  const defaults: ScopeState = {
    library: '', version: '', ecosystem: '', preset: 'balanced', limit: 8, resultLimit: 50,
    includePrerelease: false,
  };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const merged = { ...defaults, ...JSON.parse(raw) };
      merged.resultLimit = Math.min(RESULT_LIMIT_MAX, Math.max(RESULT_LIMIT_MIN, Number(merged.resultLimit) || 50));
      return merged;
    }
  } catch {
    // Corrupt storage never blocks the page; start from defaults.
  }
  return defaults;
}

export const scopeStore = reactive<ScopeState>(initial());

watch(
  scopeStore,
  (state) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Storage quota issues stay silent; the filter keeps working in memory.
    }
  },
  { deep: true },
);

export function setLibrary(name: string): void {
  scopeStore.library = name;
  scopeStore.version = '';
}

export function scopeParams(): Record<string, string | number | undefined> {
  return {
    library: scopeStore.library || undefined,
    version: scopeStore.version || undefined,
    ecosystem: scopeStore.ecosystem || undefined,
  };
}
