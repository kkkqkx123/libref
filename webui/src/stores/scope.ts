// Global library/version/ecosystem filter shared by search, answer, and insights.
import { reactive, watch } from 'vue';

const STORAGE_KEY = 'libref.scope_filter';

export type SearchPreset = 'precise' | 'balanced' | 'thorough';

interface ScopeState {
  library: string;
  version: string;
  ecosystem: string;
  preset: SearchPreset;
  limit: number;
}

function initial(): ScopeState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { library: '', version: '', ecosystem: '', preset: 'balanced', limit: 8, ...JSON.parse(raw) };
  } catch {
    // Corrupt storage never blocks the page; start from defaults.
  }
  return { library: '', version: '', ecosystem: '', preset: 'balanced', limit: 8 };
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
