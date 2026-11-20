// Mirrors normalize_rating / rating_allowed in cleanplex/scanner.py (#149, #219): Plex says "Not Rated"
// or "Unrated" where the scan list says "NR", and country ratings ("eg/16") map onto the US scale,
// so both sides go through the same mapping.
const ALIASES: Record<string, string> = { 'not rated': 'NR', unrated: 'NR', nr: 'NR' }
const COUNTRY = /^[a-z]{2}\/(.+)$/i
const COUNTRY_WORDS: Record<string, string> = { g: 'G', pg: 'PG', 'pg-13': 'PG-13', r: 'R', 'nc-17': 'NC-17', x: 'X' }

// Minimum viewing age to a US rating; 16+ is R, the closest US adult rating.
function ageToUs(age: number): string {
  if (age <= 6) return 'G'
  if (age <= 10) return 'PG'
  if (age <= 15) return 'PG-13'
  return 'R'
}

export function normalizeRating(rating: string | null | undefined): string {
  const clean = (rating ?? '').trim()
  const m = COUNTRY.exec(clean)
  if (m) {
    const body = m[1].trim().replace(/\+$/, '')
    if (/^\d+$/.test(body)) return ageToUs(Number(body))
    return COUNTRY_WORDS[body.toLowerCase()] ?? 'NR'
  }
  return ALIASES[clean.toLowerCase()] ?? clean
}

/** True if the rating passes the scan list; an empty list means "scan everything". */
export function ratingAllowed(rating: string | null | undefined, scanRatings: string[]): boolean {
  if (scanRatings.length === 0) return true
  return scanRatings.map(normalizeRating).includes(normalizeRating(rating))
}
