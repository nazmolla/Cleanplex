// The database stores SQLite CURRENT_TIMESTAMP values: UTC with no zone marker
// ("2026-11-11 14:32:12"). new Date() would read those as local time, so mark them UTC here.
const ZONE_MARKER = /(Z|[+-]\d{2}:?\d{2})$/i

/** Parse a database timestamp as the UTC instant it is; values that carry a zone are kept as-is. */
export function parseDbTimestamp(value: string): Date {
  const iso = value.replace(' ', 'T')
  return new Date(ZONE_MARKER.test(iso) ? iso : iso + 'Z')
}
