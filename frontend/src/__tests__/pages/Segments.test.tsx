import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Segments from '../../pages/Segments'
import { emitLive } from '../../test/fakeEventSource'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import { api } from '../../api/client'
const mockApi = api as unknown as Record<'get' | 'post' | 'delete', ReturnType<typeof vi.fn>>

const idle = { active_scans: [], workers_active: 0, workers_configured: 2 }
let scanner: object = idle
let segmentCount = 2

function segs(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    id: i + 1, plex_guid: 'g1', title: 'Movie A', start_ms: i * 1000, end_ms: i * 1000 + 500,
    confidence: 0.9, thumbnail_url: '', created_at: '', labels: '', category: 'nudity',
    severity: 'high', action: 'skip', channel: 'both', language: '', source: 'scanner',
  }))
}

beforeEach(() => {
  // The test environment has no Web Storage; the page only reads a UI preference from it.
  const store: Record<string, string> = {}
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => { store[k] = v },
    removeItem: (k: string) => { delete store[k] },
  })
  vi.useFakeTimers({ shouldAdvanceTime: true })
  scanner = idle
  segmentCount = 2
  mockApi.get.mockImplementation((path: string) => {
    if (path === '/api/libraries') return Promise.resolve({ libraries: [{ id: '1', title: 'Movies', type: 'movie' }] })
    if (path.includes('scanner-status')) return Promise.resolve(scanner)
    if (path.includes('/titles') && path.startsWith('/api/libraries'))
      return Promise.resolve({ titles: [{ plex_guid: 'g1', title: 'Movie A', segment_count: segmentCount }] })
    if (path.includes('/segments')) return Promise.resolve({ segments: segs(segmentCount) })
    return Promise.resolve({})
  })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('Segments live refresh (#81, #89)', () => {
  it('reloads the open title once its rescan finishes', async () => {
    render(<MemoryRouter><Segments /></MemoryRouter>)
    fireEvent.click((await screen.findAllByRole('button', { name: /Movies/ }))[0])
    fireEvent.click((await screen.findAllByRole('button', { name: /Movie A/ }))[0])
    await waitFor(() => expect(screen.getAllByText('2').length).toBeGreaterThan(0))
    const segCallsBefore = mockApi.get.mock.calls.filter(c => String(c[0]).startsWith('/api/titles/')).length

    segmentCount = 30
    await act(async () => {
      emitLive('segments_changed', { guid: 'g1' })
      await vi.advanceTimersByTimeAsync(1100)
    })

    await waitFor(() => {
      const segCalls = mockApi.get.mock.calls.filter(c => String(c[0]).startsWith('/api/titles/')).length
      expect(segCalls).toBeGreaterThan(segCallsBefore)
      expect(screen.getAllByText('30').length).toBeGreaterThan(0)
    })
  })
})
