// Theme state: light/dark persisted locally, applied as a document class.
import { ref, watch } from 'vue';

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'libref.theme';

function initial(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'dark' || saved === 'light') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export const theme = ref<Theme>(initial());

function apply(next: Theme): void {
  document.documentElement.classList.toggle('dark', next === 'dark');
}

apply(theme.value);

watch(theme, (next) => {
  localStorage.setItem(STORAGE_KEY, next);
  apply(next);
});

export function toggleTheme(): void {
  theme.value = theme.value === 'light' ? 'dark' : 'light';
}
