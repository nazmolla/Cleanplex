import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from '../../pages/Dashboard'
import { emitLive } from '../../test/fakeEventSource'

// Mock the api module so tests don't make real HTTP calls
vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }

const emptySessions = { sessions: [] }
const emptyEvents = { events: [], total: 0 }
const scannerIdle = {
  queue_size: 0,
  current_scan: null,
  current_title: null,
  current_progress: 0,
  current_scans: [],
  active_scans: [],
  workers_configured: 2,
  workers_active: 0,
  workers_idle: 2,
  paused: false,
}

function renderDashboard() {
  return render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>
  )
}

beforeEach(() => {
  // shouldAdvanceTime lets the real clock keep moving under the fake timers, so
  // testing-library's waitFor can still poll. Without it every waitFor in this
  // file hangs until the 5s test timeout.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('skips/history')) return Promise.resolve(emptyEvents)
    if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
    return Promise.resolve(emptySessions)
  })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Dashboard', () => {
  it('renders the page heading', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Dashboard')).toBeInTheDocument())
  })

  it('shows "No active streams" when sessions list is empty', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('No active streams')).toBeInTheDocument())
  })

  it('shows an empty state for the default 7-day window when there are no skips', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('No skips in this date range')).toBeInTheDocument())
  })

  it('displays active session info', async () => {
    const session = {
      session_key: 's1',
      user: 'alice',
      title: 'Inception',
      media_type: 'movie',
      position_ms: 30000,
      duration_ms: 7200000,
      client: 'Plex Web',
      is_controllable: true,
      filtering_enabled: true,
      thumb_url: '',
    }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('skips/history')) return Promise.resolve(emptyEvents)
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      return Promise.resolve({ sessions: [session] })
    })
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Inception')).toBeInTheDocument())
    expect(screen.getByText(/alice/)).toBeInTheDocument()
  })

  it('shows scanner scanning status when active scans present', async () => {
    const scannerActive = {
      ...scannerIdle,
      active_scans: [{ guid: 'g1', title: 'Some Movie', progress: 0.5, status: 'scanning' }],
    }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('skips/history')) return Promise.resolve(emptyEvents)
      if (path.includes('scanner-status')) return Promise.resolve(scannerActive)
      return Promise.resolve(emptySessions)
    })
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Some Movie')).toBeInTheDocument())
  })

  it('shows paused badge when scanner is paused', async () => {
    const paused = { ...scannerIdle, paused: true }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('skips/history')) return Promise.resolve(emptyEvents)
      if (path.includes('scanner-status')) return Promise.resolve(paused)
      return Promise.resolve(emptySessions)
    })
    renderDashboard()
    await waitFor(() => expect(screen.getByText(/Paused/)).toBeInTheDocument())
  })

  it('refreshes streams when the backend reports a change, not on a 5 s poll', async () => {
    renderDashboard()
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    const sessionCalls = () => mockApi.get.mock.calls.filter(([p]: [string]) => p === '/api/sessions').length
    const before = sessionCalls()

    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(sessionCalls()).toBe(before)

    await act(async () => {
      emitLive('sessions_changed')
      await vi.advanceTimersByTimeAsync(300)
    })
    expect(sessionCalls()).toBe(before + 1)
  })

  it('still refreshes on the slow fallback if the stream is quiet', async () => {
    renderDashboard()
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    const sessionCalls = () => mockApi.get.mock.calls.filter(([p]: [string]) => p === '/api/sessions').length
    const before = sessionCalls()

    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(sessionCalls()).toBe(before + 1)
  })

  it('aborts in-flight requests when component unmounts', async () => {
    const abortSpy = vi.spyOn(AbortController.prototype, 'abort')
    const { unmount } = renderDashboard()
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    unmount()
    expect(abortSpy).toHaveBeenCalled()
  })
})
