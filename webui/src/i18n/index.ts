import { computed, ref } from 'vue';
import { zhCN, type MessageCatalog } from './locales/zh-CN';
import { en } from './locales/en';

export type Locale = 'zh-CN' | 'en';

const STORAGE_KEY = 'libref.locale';

const catalogs: Record<Locale, MessageCatalog> = {
  'zh-CN': zhCN,
  en,
};

function detectInitialLocale(): Locale {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get('lang');
    if (fromQuery && isSupported(fromQuery)) return fromQuery as Locale;
  } catch {
    // ignore
  }
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && isSupported(saved)) return saved as Locale;
  } catch {
    // ignore
  }
  try {
    for (const candidate of navigator.languages || []) {
      const normalized = candidate.toLowerCase();
      if (normalized.startsWith('zh')) return 'zh-CN';
      if (normalized.startsWith('en')) return 'en';
    }
  } catch {
    // ignore
  }
  return 'zh-CN';
}

function isSupported(value: string): value is Locale {
  return value === 'zh-CN' || value === 'en';
}

export const locale = ref<Locale>(detectInitialLocale());

export const messages = computed<MessageCatalog>(() => catalogs[locale.value]);

export function useMessages(): MessageCatalog {
  return messages.value;
}

export function setLocale(next: Locale): void {
  if (!isSupported(next)) return;
  locale.value = next;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // ignore
  }
  document.documentElement.lang = next;
}

export function supportedLocales(): Locale[] {
  return ['zh-CN', 'en'];
}
