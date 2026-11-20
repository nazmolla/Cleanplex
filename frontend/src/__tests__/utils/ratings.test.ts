import { describe, it, expect } from 'vitest'
import { normalizeRating, ratingAllowed } from '../../utils/ratings'

describe('ratings (#149)', () => {
  it.each([
    ['Not Rated', 'NR'], ['not rated', 'NR'], ['  Unrated ', 'NR'], ['NR', 'NR'],
    ['TV-14', 'TV-14'], [' PG-13 ', 'PG-13'], ['', ''], [null, ''], [undefined, ''],
  ])('normalizes %s to %s', (raw, expected) => {
    expect(normalizeRating(raw as string | null | undefined)).toBe(expected)
  })

  it('lets every spelling of unrated through when NR is listed', () => {
    for (const r of ['Not Rated', 'Unrated', 'NR']) expect(ratingAllowed(r, ['R', 'NR'])).toBe(true)
  })

  it('still needs other ratings listed verbatim, and an empty list allows all', () => {
    expect(ratingAllowed('TV-14', ['R', 'NR'])).toBe(false)
    expect(ratingAllowed('TV-14', ['TV-14'])).toBe(true)
    expect(ratingAllowed('anything', [])).toBe(true)
    expect(ratingAllowed('', ['R', 'NR'])).toBe(false)
  })

  it.each([
    ['eg/G', 'G'], ['eg/8', 'PG'], ['eg/12', 'PG-13'], ['eg/16', 'R'], ['eg/18', 'R'], ['eg/R', 'R'], ['eg/Egypt', 'NR'],
  ])('maps the Egyptian rating %s to %s (#219)', (raw, expected) => {
    expect(normalizeRating(raw)).toBe(expected)
  })
})
