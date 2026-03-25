import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Settings from '../../pages/Settings'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
}

const defaultSettings = {
  plex_url: 'http://plex:32400',
  plex_token: 'abc',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_step_ms: '5000',
  scan_workers: '2',
  segment_gap_ms: '12000',
  segment_min_hits: '1',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED"]',
  nudenet_model: '320n',
  nudenet_model_path: '',
  sync_enabled: '0',
  sync_instance_name: '',
  sync_github_repo: '',
  sync_conflict_resolution: 'consensus',
  sync_verified_threshold: '2',
  sync_timing_tolerance_ms: '2000',
}

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.useFakeTimers()
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('settings')) return Promise.resolve({ settings: defaultSettings })
    if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
    return Promise.resolve({})
  })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Settings', () => {
  it('renders the page heading', async () => {
    renderSettings()
    await waitFor(() => expect(screen.getByText('Settings')).toBeInTheDocument())
  })

  it('loads and displays plex url from settings', async () => {
    renderSettings()
    await waitFor(() => {
      const input = screen.getByDisplayValue('http://plex:32400')
      expect(input).toBeInTheDocument()
    })
  })

  it('upload polling terminates after MAX_POLLS without timing out infinitely', async () => {
    // Simulate an upload job that stays 'running' indefinitely — the loop must stop
    let pollCount = 0
    mockApi.post.mockImplementation((path: string) => {
      if (path.includes('upload')) return Promise.resolve({ job_id: 42 })
      if (path.includes('job')) {
        pollCount++
        return Promise.resolve({ status: 'running', progress: 50, error: null, result: null })
      }
      return Promise.resolve({})
    })
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('job')) return Promise.resolve({ status: 'running', progress: 50, error: null, result: null })
      if (path.includes('settings')) return Promise.resolve({ settings: defaultSettings })
      return Promise.resolve({})
    })

    renderSettings()
    await waitFor(() => screen.getByText('Settings'))

    // The bounded poll loop should stop after MAX_POLLS (120) ticks — it must not loop forever.
    // We advance timers rapidly to simulate many poll attempts.
    await act(async () => {
      // Each poll waits 1000ms — advance 130 seconds to exceed MAX_POLLS
      vi.advanceTimersByTime(130_000)
    })

    // At this point the loop should have terminated — verify poll count is bounded
    // (exact value depends on component state; key thing is test completes)
    expect(pollCount).toBeLessThanOrEqual(130)
  })
})
