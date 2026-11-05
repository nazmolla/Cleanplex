import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Library from '../../pages/Library'
import { emitLive } from '../../test/fakeEventSource'

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
      expect(screen.getByRole('button', { name: /^Pending/ })).toHaveTextContent('Pending1')
    })

    it('does not count titles the list hides', async () => {
      await openLibrary()

      expect(screen.queryByText('Ignored Movie')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^Scanning/ })).not.toHaveTextContent('Scanning1')
      expect(screen.getByRole('button', { name: /^All/ })).toHaveTextContent('All3')
    })
  })

  it('reloads titles when a scan in this library finishes (#88, #89)', async () => {
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))
    const titleCalls = () => mockApi.get.mock.calls.filter(([p]: [string]) => p.includes('/titles')).length
    const before = titleCalls()

    await act(async () => {
      emitLive('scan_finished', { guid: 'g1' })
      emitLive('scan_finished', { guid: 'not-in-this-library' })
      await vi.advanceTimersByTimeAsync(600)
    })

    expect(titleCalls()).toBe(before + 1)
  })

  it('refetches an open segment list when a scan changes its count (#90)', async () => {
    const seg = (id: number) => ({
      id, plex_guid: 'g1', title: 'Movie A', start_ms: id * 60_000, end_ms: id * 60_000 + 5000,
      confidence: 0.9, thumbnail_url: '', created_at: '', labels: '', category: 'nudity',
      severity: 'high', action: 'skip', channel: 'both', language: '', source: 'scanner',
    })
    let count = 1
    const scanningStatus = { ...scannerIdle, active_scans: [{ guid: 'g1', title: 'Movie A', progress: 0.5, status: 'scanning' }] }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve({ ...scanningStatus })
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.startsWith('/api/titles/')) return Promise.resolve({ segments: [seg(1)] })
      if (path.includes('titles')) return Promise.resolve({ titles: [{ ...titles[0], segment_count: count }] })
      return Promise.resolve({})
    })
    mockApi.post.mockImplementation((path: string) =>
      Promise.resolve(path.includes('segments/batch') ? { segments: [seg(1), seg(2)] } : { ok: true }))
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    fireEvent.click(await screen.findByTitle('Toggle segments'))
    await waitFor(() => expect(screen.getByText('00:01:00')).toBeInTheDocument())
    expect(screen.queryByText('00:02:00')).not.toBeInTheDocument()

    count = 2
    await act(async () => {
      emitLive('segments_changed', { guid: 'g1' })
      await vi.advanceTimersByTimeAsync(600)
    })

    await waitFor(() => expect(screen.getByText('00:02:00')).toBeInTheDocument())
    const batchCalls = mockApi.post.mock.calls.filter(([p]: [string]) => p.includes('segments/batch'))
    expect(batchCalls).toHaveLength(1)
  })

  it('reloads titles when something is queued, so it shows as Pending (#104)', async () => {
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))
    const titleCalls = () => mockApi.get.mock.calls.filter(([p]: [string]) => p.includes('/titles')).length
    const before = titleCalls()

    await act(async () => {
      emitLive('scanner_changed')
      await vi.advanceTimersByTimeAsync(600)
    })

    expect(titleCalls()).toBe(before + 1)
  })

  it('keeps the fold toggle on an open title whose segments were all removed (#110)', async () => {
    let count = 2
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.startsWith('/api/titles/')) return Promise.resolve({ segments: [] })
      if (path.includes('titles')) return Promise.resolve({ titles: [{ ...titles[0], segment_count: count }] })
      return Promise.resolve({})
    })
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    fireEvent.click(await screen.findByTitle('Toggle segments'))

    // Every segment deleted or marked a false positive: the count drops to 0.
    count = 0
    await act(async () => {
      emitLive('segments_changed', { guid: 'g1' })
      await vi.advanceTimersByTimeAsync(600)
    })
    await waitFor(() => expect(screen.getByText('Movie A')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle('Toggle segments'))
    // Folded at 0 the toggle stays, so the title can be opened again (#112).
    expect(screen.getByTitle('Toggle segments')).toBeInTheDocument()
  })

  it('shows the toggle at 0 segments with the false-positive count (#112)', async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.includes('titles')) return Promise.resolve({ titles: [
        { ...titles[0], segment_count: 0, false_positive_count: 3 },
        { ...titles[1], segment_count: 0 },
      ] })
      return Promise.resolve({})
    })
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie B'))
    expect(screen.getByTitle('Toggle segments (3 marked false positive)')).toHaveTextContent(/^0\s*3$/)
    expect(screen.getByTitle('Toggle segments')).toHaveTextContent('0')
  })

  it('shows running scans as one line of title and percent (#110)', async () => {
    const status = { ...scannerIdle, active_scans: [
      { guid: 'g1', title: 'Movie A', progress: 0.42, status: 'scanning' },
      { guid: 'g9', title: 'Other Film', progress: 0.07, status: 'scanning' },
    ] }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(status)
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.includes('titles')) return Promise.resolve({ titles })
      return Promise.resolve({})
    })
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => {
      fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } })
    })
    await waitFor(() => expect(screen.getByText('Other Film')).toBeInTheDocument())
    expect(screen.getByText('7%')).toBeInTheDocument()
    expect(screen.queryByText('Scanning now')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Refresh$/ })).not.toBeInTheDocument()
  })
})


describe('Library: Not Rated and failed titles (#149, #150)', () => {
  const tv = (over: object) => ({ ...titles[0], ...over })

  async function open(items: object[], scanRatings = '["R","NR"]') {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path === '/api/settings') return Promise.resolve({ plex_url: '', scan_ratings: scanRatings })
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.includes('titles')) return Promise.resolve({ titles: items })
      return Promise.resolve({})
    })
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => { fireEvent.change(screen.getByRole('combobox'), { target: { value: 'lib1' } }) })
  }

  it('lists a pending Not Rated title when NR is in the scan list', async () => {
    await open([tv({ plex_guid: 'nr', title: 'Asfour El Sat7', status: 'pending', content_rating: 'Not Rated' })])
    expect(await screen.findByText('Asfour El Sat7')).toBeInTheDocument()
  })

  it('still hides a pending title whose rating is not listed', async () => {
    await open([tv({ plex_guid: 'nr', title: 'Asfour El Sat7', status: 'pending', content_rating: 'Not Rated' }),
                tv({ plex_guid: 'k', title: 'Kids Movie', status: 'pending', content_rating: 'G' })], '["R","NR"]')
    await screen.findByText('Asfour El Sat7')
    expect(screen.queryByText('Kids Movie')).not.toBeInTheDocument()
  })

  it('retries the failed titles of the library and reports what the server did', async () => {
    mockApi.post.mockResolvedValue({ ok: true, queued: 3, ignored_cleared: 2, skipped_ineligible: 1 })
    await open([tv({ plex_guid: 'f', title: 'Broken Movie', status: 'failed' })])
    fireEvent.click(await screen.findByRole('button', { name: /^Failed/ }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry failed' }))

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/rescan', { scope: 'failed', library_id: 'lib1' }))
    expect(await screen.findByText('3 failed titles queued for another try · 2 ignored cleared from the list · 1 outside your rating filter')).toBeInTheDocument()
  })

  it('only offers the retry on the Failed tab', async () => {
    await open([tv({ plex_guid: 'f', title: 'Broken Movie', status: 'failed' })])
    await screen.findByText('Broken Movie')
    expect(screen.queryByRole('button', { name: 'Retry failed' })).not.toBeInTheDocument()
  })

  it('shows the error when the retry fails', async () => {
    mockApi.post.mockRejectedValue(new Error('503 down'))
    await open([tv({ plex_guid: 'f', title: 'Broken Movie', status: 'failed' })])
    fireEvent.click(await screen.findByRole('button', { name: /^Failed/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Retry failed' }))
    expect(await screen.findByText(/Could not retry the failed titles: 503 down/)).toBeInTheDocument()
  })
})


describe('Library: date and cast filters, rejected segments (#177, #179, #180)', () => {
  const rich = [
    { plex_guid: 'a', rating_key: '1', title: 'Alpha', status: 'done', progress: 1,
      finished_at: '2026-11-01 10:00:00', thumb_url: '', segment_count: 0, false_positive_count: 0,
      content_rating: 'R', media_type: 'movie', year: 2015, cast: ['Jane Roe'], ignored: false },
    { plex_guid: 'b', rating_key: '2', title: 'Bravo', status: 'done', progress: 1,
      finished_at: '2026-11-04 10:00:00', thumb_url: '', segment_count: 0, false_positive_count: 1,
      content_rating: 'R', media_type: 'movie', year: 2020, cast: ['John Doe'], ignored: false },
  ]
  let currentTitles: any[] = rich

  beforeEach(() => {
    currentTitles = JSON.parse(JSON.stringify(rich))
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.endsWith('/segments')) return Promise.resolve({ segments: [] })
      if (path.includes('titles')) return Promise.resolve({ titles: JSON.parse(JSON.stringify(currentTitles)) })
      return Promise.resolve({})
    })
  })

  async function open() {
    renderLibrary()
    await waitFor(() => screen.getAllByText('Movies')[0])
    await act(async () => { fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'lib1' } }) })
    await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument())
  }
  const sortSelect = () => screen.getAllByRole('combobox').find(c => (c as HTMLSelectElement).value === 'date-added')!

  it('filters by scan date range only while sorted by scanned date', async () => {
    await open()
    expect(screen.queryByLabelText('Scanned from')).toBeNull()
    fireEvent.change(sortSelect(), { target: { value: 'scanned' } })
    fireEvent.change(screen.getByLabelText('Scanned from'), { target: { value: '2026-11-03' } })
    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull())
    expect(screen.getByText('Bravo')).toBeInTheDocument()
  })

  it('filters by release year range', async () => {
    await open()
    fireEvent.change(sortSelect(), { target: { value: 'year' } })
    fireEvent.change(screen.getByLabelText('Released from year'), { target: { value: '2019' } })
    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull())
    fireEvent.change(screen.getByLabelText('Released to year'), { target: { value: '2019' } })
    await waitFor(() => expect(screen.queryByText('Bravo')).toBeNull())
  })

  it('filters the list by actor', async () => {
    await open()
    fireEvent.change(screen.getByLabelText('Filter by actor'), { target: { value: 'jane' } })
    await waitFor(() => expect(screen.queryByText('Bravo')).toBeNull())
    expect(screen.getByText('Alpha')).toBeInTheDocument()
  })

  it('refetches an open list when only the false-positive count changes', async () => {
    currentTitles[1].false_positive_count = 0
    await open()
    fireEvent.click(screen.getAllByTitle('Toggle segments')[0])
    await waitFor(() => expect(screen.getByText('No segments found')).toBeInTheDocument())
    mockApi.post.mockResolvedValue({ segments: [{ id: 9, plex_guid: 'b', title: 'Bravo', start_ms: 0, end_ms: 1000,
      confidence: 0.9, has_thumbnail: false, thumbnail_url: '', created_at: '', labels: '', false_positive: true, auto_rejected: true }] })
    currentTitles[1].false_positive_count = 1
    await act(async () => {
      emitLive('segments_changed', { guid: 'b' })
      await vi.advanceTimersByTimeAsync(600)
    })
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/titles/segments/batch', { guids: ['b'] }))
  })
})

describe('Library: scan a show or a season (#187)', () => {
  const ep = (guid: string, season: string, name: string) => ({
    plex_guid: guid, rating_key: guid, title: `Big Show – ${season} – ${name}`, status: 'done', progress: 1,
    finished_at: null, thumb_url: '', poster_url: '', show_guid: 'show-1', show_title: 'Big Show',
    segment_count: 0, false_positive_count: 0, content_rating: 'TV-MA', media_type: 'episode', year: 2020,
    ignored: false,
  })
  const episodes = [ep('e1', 'Season 1', 'Pilot'), ep('e2', 'Season 2', 'Return')]

  beforeEach(() => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path.includes('libraries') && !path.includes('titles')) {
        return Promise.resolve({ libraries: [{ id: 'tv', title: 'TV Shows', type: 'show' }] })
      }
      if (path.includes('titles')) return Promise.resolve({ titles: JSON.parse(JSON.stringify(episodes)) })
      return Promise.resolve({})
    })
    mockApi.post.mockResolvedValue({ ok: true, queued: 1, already_scanning: 0 })
  })

  async function openShow() {
    renderLibrary()
    await waitFor(() => screen.getAllByText('TV Shows')[0])
    await act(async () => { fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'tv' } }) })
    await screen.findByText('Big Show')
  }

  it('queues the whole show for tonight from the show header', async () => {
    await openShow()
    fireEvent.click(screen.getByRole('button', { name: 'Scan whole show tonight' }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/show', {
      show_guid: 'show-1', show_title: 'Big Show', season: '', library_id: 'tv', now: false,
    }))
    expect(await screen.findByText(/Big Show: 1 episode queued for tonight/)).toBeInTheDocument()
  })

  it('queues one season now from the season header', async () => {
    await openShow()
    fireEvent.click(screen.getByTitle('Expand show'))
    fireEvent.click(screen.getByRole('button', { name: 'Scan Season 2 now' }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/show', {
      show_guid: 'show-1', show_title: 'Big Show', season: 'Season 2', library_id: 'tv', now: true,
    }))
    expect(await screen.findByText(/Big Show · Season 2: 1 episode queued to scan now/)).toBeInTheDocument()
  })

  it('reports the error when queueing a show fails', async () => {
    mockApi.post.mockRejectedValue(new Error('404 nothing'))
    await openShow()
    fireEvent.click(screen.getByRole('button', { name: 'Scan whole show now' }))
    expect(await screen.findByText(/Could not queue Big Show: 404 nothing/)).toBeInTheDocument()
  })
})
