import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import DetectionAccuracy from '../../components/DetectionAccuracy'
import { emitLive } from '../../test/fakeEventSource'

vi.mock('../../api/client', () => ({ api: { get: vi.fn() } }))

import { api } from '../../api/client'
const mockGet = api.get as ReturnType<typeof vi.fn>

afterEach(() => vi.clearAllMocks())

const row = (over: object) => ({
  source: 'siglip', detector: 'siglip-base-224', label: 'BUTTOCKS_EXPOSED', total: 10,
  false_positives: 4, false_positive_rate: 0.4, avg_score_kept: 0.9, avg_score_false: 0.62, ...over,
})

describe('DetectionAccuracy (#101)', () => {
  it('shows each detector and label with its false-positive rate and scores', async () => {
    mockGet.mockResolvedValue({ rows: [row({}), row({ source: 'scanner', detector: 'nudenet-640m', false_positives: 0, false_positive_rate: 0, avg_score_false: null })], marked: 4 })
    render(<DetectionAccuracy />)

    await waitFor(() => expect(screen.getByText('SigLIP')).toBeInTheDocument())
    expect(screen.getByText('NudeNet 640m')).toBeInTheDocument()
    expect(screen.getByText('40%')).toBeInTheDocument()
    expect(screen.getByText('62%')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('explains how to start when nothing is marked yet', async () => {
    mockGet.mockResolvedValue({ rows: [row({ false_positives: 0, false_positive_rate: 0 })], marked: 0 })
    render(<DetectionAccuracy />)

    await waitFor(() => expect(screen.getByText(/No false positives marked yet/)).toBeInTheDocument())
  })

  it('refreshes when segments change', async () => {
    mockGet.mockResolvedValue({ rows: [row({})], marked: 4 })
    render(<DetectionAccuracy />)
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    emitLive('segments_changed', { guid: 'g' })

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2), { timeout: 3000 })
  })
})
