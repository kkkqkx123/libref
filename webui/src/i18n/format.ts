import { computed } from 'vue';
import { locale } from './index';

export function useFormatter() {
  const numberFormat = computed(() => new Intl.NumberFormat(locale.value));
  const dateFormat = computed(() => new Intl.DateTimeFormat(locale.value));
  return {
    number: (value: number) => numberFormat.value.format(value),
    date: (value: Date | number | string) => dateFormat.value.format(new Date(value)),
  };
}
