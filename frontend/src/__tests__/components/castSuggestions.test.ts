import { describe, it, expect } from 'vitest'
import { castSuggestionsFor } from '../../components/browse/FilterPanel'

const titles = [
  { cast: ['Zoe', 'Adam', 'Mia'], segment_count: 1 },
  { cast: ['Zoe', 'Adam'], segment_count: 0 },
  { cast: ['Zoe', 'Mia'], segment_count: 6 },
  { cast: ['Solo'], segment_count: 9 },
]
const names = (s: { name: string }[]) => s.map(c => c.name)

describe('castSuggestionsFor (#227)', () => {
  it('keeps most titles first by default', () => {
    expect(names(castSuggestionsFor(titles, 'titles'))).toEqual(['Zoe', 'Adam', 'Mia'])
  })
  it('orders by name', () => {
    expect(names(castSuggestionsFor(titles, 'name'))).toEqual(['Adam', 'Mia', 'Zoe'])
  })
  it('orders by segments across all titles, including single-title actors', () => {
    const s = castSuggestionsFor(titles, 'segments')
    expect(names(s)).toEqual(['Solo', 'Mia', 'Zoe', 'Adam'])
    expect(s[1]).toEqual({ name: 'Mia', titles: 2, segments: 7 })
  })
})
