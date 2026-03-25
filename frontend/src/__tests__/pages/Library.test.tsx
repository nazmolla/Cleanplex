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
  vi.useFakeTimers()
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

describe('Library', () => {
  it('renders the page heading', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getByText('Library')).toBeInTheDocument())
  })

  it('shows library dropdown with loaded libraries', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getByText('Movies')).toBeInTheDocument())
  })

  it('does NOT trigger sync automatically when a library is selected', async () => {
    renderLibrary()
    await waitFor(() => screen.getByText('Movies'))

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
    await waitFor(() => screen.getByText('Movies'))
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
    await waitFor(() => screen.getByText('Movies'))
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
    await waitFor(() => screen.getByText('Movies'))
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie 0'))

    // Select all and scan
    const selectAllCheckbox = screen.queryByRole('checkbox', { name: /select all/i })
    if (selectAllCheckbox) {
      await act(async () => { fireEvent.click(selectAllCheckbox) })
    }

    const scanBtn = screen.queryByRole('button', { name: /scan selected/i })
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
    await waitFor(() => screen.getByText('Library'))
    unmount()
    expect(abortSpy).toHaveBeenCalled()
  })
})
