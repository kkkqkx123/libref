// Locale-aware number, date and relative-time formatting. Backend timestamps
// arrive either as ISO strings or as naive UTC text (`YYYY-MM-DD HH:MM:SS`).
import { computed } from 'vue';
import { locale } from './index';

const NAIVE_UTC = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/;

export function parseTimestamp(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') {
    return new Date(value < 1e12 ? value * 1000 : value);
  }
  const match = NAIVE_UTC.exec(value.trim());
  if (match) {
    const [, year, month, day, hour, minute, second] = match;
    return new Date(
      Date.UTC(
        Number(year),
        Number(month) - 1,
        Number(day),
        Number(hour),
        Number(minute),
        Number(second),
      ),
    );
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

const RELATIVE_STEPS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 30],
  ['month', 12],
  ['year', Number.POSITIVE_INFINITY],
];

function relativeParts(date: Date, now: Date): [number, Intl.RelativeTimeFormatUnit] {
  let delta = (date.getTime() - now.getTime()) / 1000;
  let unit: Intl.RelativeTimeFormatUnit = 'second';
  for (const [candidate, step] of RELATIVE_STEPS) {
    unit = candidate;
    if (Math.abs(delta) < step) break;
    delta /= step;
  }
  return [Math.round(delta), unit];
}

export function useFormatters() {
  const numberFormat = computed(() => new Intl.NumberFormat(locale.value));
  const percentFormat = computed(
    () => new Intl.NumberFormat(locale.value, { style: 'percent', maximumFractionDigits: 1 }),
  );
  const dateTimeFormat = computed(
    () => new Intl.DateTimeFormat(locale.value, { dateStyle: 'short', timeStyle: 'medium' }),
  );
  const relativeFormat = computed(
    () => new Intl.RelativeTimeFormat(locale.value, { numeric: 'auto' }),
  );

  return {
    number: (value: number): string => numberFormat.value.format(value),
    percent: (value: number): string => percentFormat.value.format(value),
    dateTime: (value: string | number | null | undefined): string => {
      const date = parseTimestamp(value);
      if (!date) return typeof value === 'string' ? value : '';
      return dateTimeFormat.value.format(date);
    },
    relative: (value: string | number | null | undefined): string => {
      const date = parseTimestamp(value);
      if (!date) return typeof value === 'string' ? value : '';
      const [amount, unit] = relativeParts(date, new Date());
      return relativeFormat.value.format(amount, unit);
    },
  };
}
