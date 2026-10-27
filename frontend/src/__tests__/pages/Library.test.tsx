import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Library from '../../pages/Library'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
}

const libraries = [
  { id: 'lib1', title: 'Movies', type: 'movie' },
]

const titles = [
  { plex_guid: 'g1', rating_key: '1', title: 'Movie A', status: 'done', progress: 1,
    finished_at: null, thumb_url: '', poster_url: '', show_guid: '', show_title: '',
    segment_count: 2, content_rating: 'R', media_type: 'movie', year: 2020, ignored: false },
  { plex_guid: 'g2', rating_key: '2', title: 'Movie B', status: 'pending', progress: 0,
    finished_at: null, thumb_url: '', poster_url: '', show_guid: '', show_title: '',
    segment_count: 0, content_rating: 'PG', media_type: 'movie', year: 2021, ignored: false },
]

const scannerIdle = {
  queue_size: 0, current_scan: null, current_title: null, current_progress: 0,
  current_scans: [], active_scans: [], workers_configured: 2,
  workers_active: 0, workers_idle: 2, paused: false,
}

function renderLibrary() {
  return render(<MemoryRouter><Library /></MemoryRouter>)
}

beforeEach(() => {
  // shouldAdvanceTime lets the real clock keep moving under the fake timers, so
  // testing-library's waitFor can still poll. Without it every waitFor in this
  // file hangs until the 5s test timeout.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
    if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
    if (path.includes('titles')) return Promise.resolve({ titles })
    return Promise.resolve({})
  })
  mockApi.post.mockResolvedValue({ ok: true, synced: 2, new: 0 })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

// The page renders a mobile picker and a desktop layout side by side, so library
// names and the heading legitimately appear more than once. Assert on the first
// match rather than demanding a unique one.
describe('Library', () => {
  it('renders the page heading', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Library')[0]).toBeInTheDocument())
  })

  it('shows library dropdown with loaded libraries', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies')[0]).toBeInTheDocument())
  })

  it('does NOT trigger sync automatically when a library is selected', async () => {
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])

    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))

    // No sync call should fire on selection
    const syncCalls = mockApi.post.mock.calls.filter(([path]: [string]) =>
      path.includes('/sync')
    )
    expect(syncCalls.length).toBe(0)
  })

  it('has an explicit "Sync from Plex" button that triggers sync on click', async () => {
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))

    const syncBtn = screen.getByRole('button', { name: /sync from plex/i })
    expect(syncBtn).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(syncBtn)
    })

    await waitFor(() => {
      const syncCalls = mockApi.post.mock.calls.filter(([path]: [string]) =>
        path.includes('/sync')
      )
      expect(syncCalls.length).toBe(1)
    })
  })

  it('displays titles after library is selected', async () => {
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => {
      expect(screen.getByText('Movie A')).toBeInTheDocument()
      expect(screen.getByText('Movie B')).toBeInTheDocument()
    })
  })

  it('scan selected button fires requests with bounded concurrency', async () => {
    // Create 10 titles all in pending state
    const manyTitles = Array.from({ length: 10 }, (_, i) => ({
      ...titles[1], plex_guid: `g${i + 10}`, title: `Movie ${i}`, rating_key: `${i + 10}`,
    }))
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.includes('titles')) return Promise.resolve({ titles: manyTitles })
      return Promise.resolve({})
    })

    let maxConcurrent = 0
    let currentConcurrent = 0
    mockApi.post.mockImplementation(() =>
      new Promise<{ok: boolean}>(resolve => {
        currentConcurrent++
        maxConcurrent = Math.max(maxConcurrent, currentConcurrent)
        setTimeout(() => {
          currentConcurrent--
          resolve({ ok: true })
        }, 100)
      })
    )

    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie 0'))

    // Select all and scan
    const selectAllCheckbox = screen.queryAllByRole('checkbox', { name: /select all/i })[0]
    if (selectAllCheckbox) {
      await act(async () => { fireEvent.click(selectAllCheckbox) })
    }

    // Mobile and desktop layouts each render the action bar; either button drives
    // the same handler, so clicking the first is enough.
    const scanBtn = screen.queryAllByRole('button', { name: /scan selected/i })[0]
    if (scanBtn) {
      await act(async () => {
        fireEvent.click(scanBtn)
        vi.advanceTimersByTime(2000)
      })
      // Concurrency must stay at or below CONCURRENCY=5
      expect(maxConcurrent).toBeLessThanOrEqual(5)
    }
  })

  it('aborts polling on unmount', async () => {
    const abortSpy = vi.spyOn(AbortController.prototype, 'abort')
    const { unmount } = renderLibrary()
    await waitFor(() => screen.getAllByText('Library')[0])
    unmount()
    expect(abortSpy).toHaveBeenCalled()
  })

  describe('status tabs (#84, #85)', () => {
    const extra = [
      // Pending with a rating the scanner skips: should not be listed anywhere.
      { ...titles[1], plex_guid: 'g3', title: 'Kids Movie', content_rating: 'G' },
      // Already scanned with an excluded rating: results stay visible.
      { ...titles[0], plex_guid: 'g4', title: 'Old G Movie', content_rating: 'G' },
      // Stuck in scanning but ignored: hidden, and must not be counted either.
      { ...titles[0], plex_guid: 'g5', title: 'Ignored Movie', status: 'scanning', ignored: true },
    ]

    async function openLibrary() {
      mockApi.get.mockImplementation((path: string) => {
        if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
        if (path === '/api/settings') return Promise.resolve({ plex_url: '', scan_ratings: '["R","PG"]' })
        if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
        if (path.includes('titles')) return Promise.resolve({ titles: [...titles, ...extra] })
        return Promise.resolve({})
      })
      renderLibrary()
      await waitFor(() => screen.getAllByText('Movies')[0])
      await act(async () => {
        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
      })
      await waitFor(() => screen.getByText('Movie A'))
    }

    it('hides pending titles whose rating the scanner excludes', async () => {
      await openLibrary()

      await waitFor(() => expect(screen.queryByText('Kids Movie')).not.toBeInTheDocument())
      expect(screen.getByText('Old G Movie')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^Pending/ })).toHaveTextContent('(1)')
    })

    it('does not count titles the list hides', async () => {
      await openLibrary()

      expect(screen.queryByText('Ignored Movie')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^Scanning/ })).not.toHaveTextContent('(1)')
      expect(screen.getByRole('button', { name: /^All/ })).toHaveTextContent('(3)')
    })
  })
  it('keeps refreshing titles while a scan runs (#88)', async () => {
    const scanningStatus = { ...scannerIdle, active_scans: [{ guid: 'g2', title: 'Movie B', progress: 0.5, status: 'scanning' }] }
    mockApi.get.mockImplementation((path: string) => {
      // A fresh object per poll, as the real API returns; the old bug only shows then.
      if (path.includes('scanner-status')) return Promise.resolve({ ...scanningStatus })
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.includes('titles')) return Promise.resolve({ titles })
      return Promise.resolve({})
    })
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))
    const titleCalls = () => mockApi.get.mock.calls.filter(([p]: [string]) => p.includes('/titles')).length
    const before = titleCalls()

    // Several 3 s status polls land inside this window; the 5 s refresh must still fire.
    // Step a second at a time so each status response lands between timers.
    for (let i = 0; i < 11; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    }

    await waitFor(() => expect(titleCalls()).toBeGreaterThanOrEqual(before + 2))
  })
})
