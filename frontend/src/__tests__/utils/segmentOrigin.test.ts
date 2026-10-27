import { describe, it, expect } from 'vitest'
import { segmentOrigin } from '../../utils/segmentOrigin'

describe('segmentOrigin (#92)', () => {
  it('names the model that found a scanner segment', () => {
    expect(segmentOrigin({ source: 'scanner', detector: 'nudenet-640m' })).toBe('NudeNet 640m')
    expect(segmentOrigin({ source: 'scanner', detector: 'nudenet-320n' })).toBe('NudeNet 320n')
    expect(segmentOrigin({ source: 'siglip', detector: 'siglip-base-224' })).toBe('SigLIP')
  })

  it('marks a custom model file', () => {
    expect(segmentOrigin({ source: 'scanner', detector: 'nudenet-320n-custom' })).toBe('NudeNet 320n (custom)')
  })

  it('falls back to the source for imports and older rows', () => {
    expect(segmentOrigin({ source: 'scanner', detector: '' })).toBe('NudeNet')
    expect(segmentOrigin({ source: 'skp' })).toBe('VideoSkip (.skp)')
    expect(segmentOrigin({ source: 'subtitles' })).toBe('Subtitles')
    expect(segmentOrigin({})).toBe('NudeNet')
    expect(segmentOrigin({ source: 'something-new' })).toBe('something-new')
  })
})
