import { describe, it, expect } from 'vitest'
import { parseDbTimestamp } from '../../utils/dbTime'

describe('parseDbTimestamp', () => {
  it('reads a zone-less database timestamp as UTC', () => {
    expect(parseDbTimestamp('2026-11-11 14:32:12').toISOString()).toBe('2026-11-11T14:32:12.000Z')
  })

  it('keeps a value that already carries a zone', () => {
    expect(parseDbTimestamp('2026-11-11T14:32:12+02:00').toISOString()).toBe('2026-11-11T12:32:12.000Z')
    expect(parseDbTimestamp('2026-11-11T14:32:12Z').toISOString()).toBe('2026-11-11T14:32:12.000Z')
  })

  it('returns an invalid date for garbage', () => {
    expect(Number.isNaN(parseDbTimestamp('nope').getTime())).toBe(true)
  })
})
