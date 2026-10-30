// Mirrors normalize_rating / rating_allowed in cleanplex/scanner.py (#149): Plex says "Not Rated"
// or "Unrated" where the scan list says "NR", so both sides go through the same mapping.
const ALIASES: Record<string, string> = { 'not rated': 'NR', unrated: 'NR', nr: 'NR' }

export function normalizeRating(rating: string | null | undefined): string {
  const clean = (rating ?? '').trim()
  return ALIASES[clean.toLowerCase()] ?? clean
}

/** True if the rating passes the scan list; an empty list means "scan everything". */
export function ratingAllowed(rating: string | null | undefined, scanRatings: string[]): boolean {
  if (scanRatings.length === 0) return true
  return scanRatings.map(normalizeRating).includes(normalizeRating(rating))
}
