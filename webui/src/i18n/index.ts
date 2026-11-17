import { computed, ref } from 'vue';
import { zhCN, type MessageCatalog } from './locales/zh-CN';
import { en } from './locales/en';

export type Locale = 'zh-CN' | 'en';

const STORAGE_KEY = 'libref.locale';

const catalogs: Record<Locale, MessageCatalog> = {
  'zh-CN': zhCN,
  en,
};

function isSupported(value: string): value is Locale {
  return value === 'zh-CN' || value === 'en';
}

function detectInitialLocale(): Locale {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get('lang');
    if (fromQuery && isSupported(fromQuery)) return fromQuery;
  } catch {
    // A missing or unreadable location only costs the query-parameter hint.
  }
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && isSupported(saved)) return saved;
  } catch {
    // Storage may be disabled; detection falls through to browser hints.
  }
  try {
    for (const candidate of navigator.languages || []) {
      const normalized = candidate.toLowerCase();
      if (normalized.startsWith('zh')) return 'zh-CN';
      if (normalized.startsWith('en')) return 'en';
    }
  } catch {
    // No navigator hints means the built-in default wins.
  }
  return 'zh-CN';
}

export const locale = ref<Locale>(detectInitialLocale());

export const messages = computed<MessageCatalog>(() => catalogs[locale.value]);

function applyDocumentLang(next: Locale): void {
  document.documentElement.lang = next;
}

applyDocumentLang(locale.value);

export function setLocale(next: Locale): void {
  if (!isSupported(next)) return;
  // Re-selecting the active locale still persists it, so the choice survives a
  // reload even when the browser preference points elsewhere.
  locale.value = next;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Persistence is best-effort; the switch still applies for this session.
  }
  applyDocumentLang(next);
}

export function supportedLocales(): Locale[] {
  return ['zh-CN', 'en'];
}
