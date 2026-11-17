// Element Plus locale follows the active UI locale: the default pack ships in
// the initial bundle, every other pack is imported on demand.
import { ref, watch } from 'vue';
import type { Language } from 'element-plus/es/locale';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import { locale, type Locale } from './index';

const cache: Partial<Record<Locale, Language>> = { 'zh-CN': zhCn };

export const elementLocale = ref<Language>(zhCn);

async function resolve(next: Locale): Promise<Language> {
  const cached = cache[next];
  if (cached) return cached;
  const mod = next === 'en' ? await import('element-plus/es/locale/lang/en') : null;
  const value = (mod?.default ?? zhCn) as Language;
  cache[next] = value;
  return value;
}

void resolve(locale.value).then((value) => {
  elementLocale.value = value;
});

watch(locale, async (next) => {
  const value = await resolve(next);
  if (locale.value === next) elementLocale.value = value;
});
