import { ref, watch } from 'vue';
import { locale } from './index';
import zhCn from 'element-plus/es/locale/lang/zh-cn';

export const elementLocale = ref<any>(zhCn);

async function load(next: string): Promise<void> {
  if (next === 'en') {
    const mod = await import('element-plus/es/locale/lang/en');
    elementLocale.value = mod.default;
    return;
  }
  const mod = await import('element-plus/es/locale/lang/zh-cn');
  elementLocale.value = mod.default;
}

void load(locale.value);
watch(locale, (next) => {
  void load(next);
});
