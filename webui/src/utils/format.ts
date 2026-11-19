// Shared value formatting for JSON wire values.
import type { JsonValue } from '../api/types';

export function formatCell(value: JsonValue): string {
  if (value === null) return '';
  if (Array.isArray(value)) {
    if (value.length === 0) return '';
    if (value.every((item) => typeof item !== 'object')) return value.join(', ');
    return JSON.stringify(value);
  }
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
