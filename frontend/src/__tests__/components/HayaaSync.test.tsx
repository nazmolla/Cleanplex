import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import HayaaSync from '../../components/HayaaSync'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}))

import { api } from '../../api/client'
const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> }

describe('HayaaSync', () => {
  it('says why Hayaa held titles back', async () => {
    mockApi.get.mockImplementation((path: string) => Promise.resolve(path === '/api/hayaa/sync' ? {
      running: false, phase: '', auto_sync: false, interval_h: 6, preset: 'balanced',
      preset_auto_switched: false, last_sync: 1, paused_until: null,
      quota: { pushes: 0, pulls: 0, pushes_limit: 20, pulls_limit: 60 },
      last_result: { pushed: 1, retracted: 0, described: 0, pulled: 3, changed: 1,
        skipped_quota: false, error: '', deferred: { budget: 2, prove_possession: 1 } },
    } : { held: [], community_false_positives: [] }))
    render(<HayaaSync />)
    expect(await screen.findByText(/held back: 2 today's viewing allowance is used up, 1 file check sent/))
      .toBeTruthy()
  })
})
