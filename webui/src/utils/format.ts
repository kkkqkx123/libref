// Shared value formatting for heterogeneous backend rows.
export function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) {
    if (value.length === 0) return '';
    if (value.every((item) => typeof item !== 'object')) return value.join(', ');
    return JSON.stringify(value);
  }
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
