import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Browse from '../../pages/Browse'
import { emitLive } from '../../test/fakeEventSource'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn(), upload: vi.fn() },
}))

import { api } from '../../api/client'
const mockApi = api as unknown as Record<'get' | 'post' | 'delete', ReturnType<typeof vi.fn>>

const libraries = [
  { id: 'lib1', title: 'Movies', type: 'movie' },
  { id: 'lib2', title: 'TV Shows', type: 'show' },
]

const base = {
  progress: 0, finished_at: null, thumb_url: '', poster_url: '', show_guid: '', show_title: '',
  segment_count: 0, false_positive_count: 0, content_rating: 'R', media_type: 'movie', year: 2020, ignored: false, cast: [],
}
const movies = [
  { ...base, plex_guid: 'g1', rating_key: '1', title: 'Movie A', status: 'done', segment_count: 2, finished_at: '2026-11-03 10:00:00', cast: ['Ann Actor'] },
  { ...base, plex_guid: 'g2', rating_key: '2', title: 'Movie B', status: 'pending', content_rating: 'PG', year: 2021 },
  { ...base, plex_guid: 'g3', rating_key: '3', title: 'Movie C', status: 'failed', year: 1999, finished_at: '2026-09-01 10:00:00',
    error_msg: 'Could not determine video duration for: /media/films/c.mp4' },
]
const episodes = [
  { ...base, plex_guid: 'e1', rating_key: '11', title: 'The Show – Season 1 – Pilot', status: 'done', media_type: 'episode',
    show_guid: 'show-1', show_title: 'The Show', segment_count: 1 },
  { ...base, plex_guid: 'e2', rating_key: '12', title: 'The Show – Season 2 – Return', status: 'done', media_type: 'episode',
    show_guid: 'show-1', show_title: 'The Show', segment_count: 1 },
  { ...base, plex_guid: 'e3', rating_key: '13', title: 'The Show – Season 2 – Finale', status: 'pending', media_type: 'episode',
    show_guid: 'show-1', show_title: 'The Show' },
]

const scannerIdle = {
  queue_size: 0, current_scan: null, current_title: null, current_progress: 0, current_scans: [], active_scans: [],
  workers_configured: 2, workers_active: 0, workers_idle: 2, paused: false,
}

const seg = (id: number, guid: string, title: string, labels = 'FEMALE_BREAST_EXPOSED') => ({
  id, plex_guid: guid, title, start_ms: id * 60_000, end_ms: id * 60_000 + 5000, confidence: 0.8, has_thumbnail: false,
  thumbnail_url: '', created_at: '2026-11-03 10:00:00', labels, source: 'scanner', detector: 'nudenet', false_positive: false,
})

let settings: Record<string, string>
let scanner: typeof scannerIdle
let movieList: typeof movies

function renderAt(url = '/browse') {
  return render(<MemoryRouter initialEntries={[url]}><Browse /></MemoryRouter>)
}
const titleCalls = (lib: string) => mockApi.get.mock.calls.filter(([p]: [string]) => p === `/api/libraries/${lib}/titles`).length

beforeEach(() => {
  // shouldAdvanceTime lets the real clock keep moving under the fake timers, so
  // testing-library's waitFor can still poll.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  settings = { plex_url: '', scan_ratings: '[]' }
  scanner = scannerIdle
  movieList = movies
  // This jsdom setup has no localStorage (opaque origin); give each test a fresh one.
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
  })
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('scanner-status')) return Promise.resolve(scanner)
    if (path === '/api/libraries') return Promise.resolve({ libraries })
    if (path === '/api/libraries/lib1/titles') return Promise.resolve({ titles: movieList })
    if (path === '/api/libraries/lib2/titles') return Promise.resolve({ titles: episodes })
    if (path === '/api/settings') return Promise.resolve(settings)
    if (path.startsWith('/api/markers/libraries/')) return Promise.resolve({ titles: [{ ...movies[0], marker_count: 2 }], cached: true })
    if (path.startsWith('/api/markers/titles/')) return Promise.resolve({ markers: [] })
    return Promise.resolve({})
  })
  mockApi.post.mockImplementation((path: string, body: any) => {
    if (path === '/api/titles/segments/batch') {
      const all = [seg(1, 'g1', 'Movie A'), seg(2, 'g1', 'Movie A', 'BUTTOCKS_EXPOSED'), seg(3, 'e1', 'The Show – Season 1 – Pilot'), seg(4, 'e2', 'The Show – Season 2 – Return')]
      return Promise.resolve({ segments: all.filter(s => body.guids.includes(s.plex_guid)) })
    }
    return Promise.resolve({ ok: true, queued: 2, already_scanning: 0, ignored_cleared: 1, skipped_ineligible: 0 })
  })
  mockApi.delete.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

const openFilters = () => fireEvent.click(screen.getByRole('button', { name: /^Filters/ }))
// Group rows open their level; title rows expand in place (#209).
const rowNames = () => screen.getAllByTitle(/^(Open|Show segments of) /).map(b => b.textContent)

describe('Browse levels (#207, #208)', () => {
  it('lists the libraries at the top level without syncing any of them', async () => {
    renderAt()
    const sidebar = await screen.findByRole('navigation', { name: 'Libraries' })
    await waitFor(() => expect(within(sidebar).getByRole('button', { name: /^Movies/ })).toHaveTextContent('3'))
    expect(rowNames()).toEqual(['Movies', 'TV Shows'])
    expect(mockApi.post.mock.calls.some(([p]: [string]) => p.includes('/sync'))).toBe(false)
  })

  it('lists a movie library and explains a failed title with the reason the scan gave (#190)', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    expect(screen.getByText('Movie B')).toBeInTheDocument()
    expect(screen.getByText('Could not determine video duration for: c.mp4')).toBeInTheDocument()
  })

  it('opens shows, then seasons, then episodes with the same rows', async () => {
    renderAt('/browse?lib=lib2')
    fireEvent.click(await screen.findByTitle('Open The Show'))
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('The Show'))
    expect(rowNames()).toEqual(['Season 1', 'Season 2'])
    fireEvent.click(screen.getByTitle('Open Season 2'))
    await waitFor(() => expect(rowNames()).toEqual(['Finale', 'Return']))
  })

  it('expands a movie row into its segments without leaving the list (#209)', async () => {
    renderAt('/browse?lib=lib1')
    const name = await screen.findByTitle('Show segments of Movie A')
    fireEvent.click(name)
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/titles/segments/batch', { guids: ['g1'] }))
    expect(name).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('tab', { name: 'Titles' })).toHaveAttribute('aria-selected', 'true')
  })

  it('keeps the view when opening a row', async () => {
    renderAt('/browse?lib=lib2&view=markers')
    fireEvent.click(await screen.findByTitle('Open The Show'))
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('The Show'))
    expect(screen.getByRole('tab', { name: 'Markers' })).toHaveAttribute('aria-selected', 'true')
  })
})

describe('Browse search and filters (#207, #208)', () => {
  it('finds a show by its name as one row, and an episode by its name with its show as context', async () => {
    renderAt('/browse?lib=lib2')
    await screen.findByTitle('Open The Show')
    fireEvent.change(screen.getByLabelText('Filter by name'), { target: { value: 'the show' } })
    expect(rowNames()).toEqual(['The Show'])

    fireEvent.change(screen.getByLabelText('Filter by name'), { target: { value: 'pilot' } })
    expect(rowNames()).toEqual(['Pilot'])
    expect(screen.getByText('The Show · Season 1 · R')).toBeInTheDocument()
  })

  it('combines the name, scan date and year filters', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    openFilters()
    fireEvent.change(screen.getByLabelText('Scanned from'), { target: { value: '2026-10-01' } })
    expect(rowNames()).toEqual(['Movie A'])

    fireEvent.change(screen.getByLabelText('Released to year'), { target: { value: '2010' } })
    expect(screen.queryByText('Movie A')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^Filters/ }))
    fireEvent.click(screen.getByText('Clear all'))
    fireEvent.change(screen.getByLabelText('Filter by name'), { target: { value: 'movie b' } })
    expect(rowNames()).toEqual(['Movie B'])
  })

  it('filters by actor and shows the filter as a removable chip', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    openFilters()
    fireEvent.change(screen.getByLabelText('Filter by actor'), { target: { value: 'ann' } })
    expect(rowNames()).toEqual(['Movie A'])
    expect(screen.getByRole('button', { name: /^Filters/ })).toHaveTextContent('1')

    openFilters()
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter Actor: ann' }))
    expect(rowNames()).toHaveLength(3)
  })

  it('counts each status and lists only the chosen one (#84, #85)', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    openFilters()
    const failed = screen.getByRole('button', { name: /^Failed/ })
    expect(failed).toHaveTextContent('1')
    fireEvent.click(failed)
    expect(rowNames()).toEqual(['Movie C'])
  })

  it('hides pending titles whose rating the scanner excludes (#149)', async () => {
    settings = { plex_url: '', scan_ratings: '["R"]' }
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    await waitFor(() => expect(screen.queryByText('Movie B')).not.toBeInTheDocument())
    openFilters()
    expect(screen.getByRole('button', { name: /^All/, pressed: true })).toHaveTextContent('2')
  })

  it('lists a queued title whatever its rating, and an ignored one only with Show ignored (#214)', async () => {
    settings = { plex_url: '', scan_ratings: '["R"]' }
    movieList = [...movies, { ...base, plex_guid: 'g4', rating_key: '4', title: 'Movie D', status: 'pending', content_rating: 'PG', ignored: true }]
    scanner = { ...scannerIdle, queue_size: 2, queue_items: [{ guid: 'g2' }, { guid: 'g4' }] } as typeof scannerIdle
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie B')
    expect(screen.queryByText('Movie D')).not.toBeInTheDocument()
    openFilters()
    expect(screen.getByRole('button', { name: /^Pending/ })).toHaveTextContent('1')
    fireEvent.click(screen.getByLabelText('Show ignored and out-of-scope titles'))
    await screen.findByText('Movie D')
    expect(screen.getByRole('button', { name: /^Pending/ })).toHaveTextContent('2')
  })

  it('scans the selected titles from the header instead of the library (#215)', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    fireEvent.click(screen.getByLabelText('Select Movie A'))
    fireEvent.click(screen.getByRole('button', { name: 'Scan 1 selected title now' }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/title', { plex_guid: 'g1', now: true, library_id: 'lib1' }))
    expect(mockApi.post.mock.calls.some(([p]: [string]) => p.startsWith('/api/scan/library'))).toBe(false)
    expect(screen.queryByRole('button', { name: 'Scan library now' })).toBeInTheDocument()
  })

  it('hides a scanned title whose rating is out of scope until asked for (#216)', async () => {
    settings = { plex_url: '', scan_ratings: '["R"]' }
    movieList = [...movies, { ...base, plex_guid: 'g5', rating_key: '5', title: 'Movie E', status: 'done', content_rating: 'PG' }]
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    await waitFor(() => expect(screen.queryByText('Movie E')).not.toBeInTheDocument())
    openFilters()
    fireEvent.click(screen.getByLabelText('Show ignored and out-of-scope titles'))
    await screen.findByText('Movie E')
  })

  it('clears the scanned range so pending titles show again, and resets filters (#217)', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie B')
    openFilters()
    fireEvent.change(screen.getByLabelText('Scanned from'), { target: { value: '2026-11-01' } })
    await waitFor(() => expect(screen.queryByText('Movie B')).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await screen.findByText('Movie B')
    fireEvent.change(screen.getByLabelText('Scanned to'), { target: { value: '2026-11-30' } })
    fireEvent.click(screen.getByLabelText('Show ignored and out-of-scope titles'))
    fireEvent.click(screen.getByRole('button', { name: 'Reset filters' }))
    await screen.findByText('Movie B')
    expect(screen.getByLabelText('Scanned to')).toHaveValue('')
    expect(screen.getByLabelText('Show ignored and out-of-scope titles')).not.toBeChecked()
  })

  it('names the sort direction and reverses the order', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    expect(rowNames()).toEqual(['Movie C', 'Movie B', 'Movie A'])
    fireEvent.click(screen.getByRole('button', { name: 'Sort direction: Newest first' }))
    expect(screen.getByRole('button', { name: 'Sort direction: Oldest first' })).toBeInTheDocument()
    expect(rowNames()).toEqual(['Movie A', 'Movie B', 'Movie C'])
  })

  it('counts in the sidebar only the titles that pass the filters', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    const sidebar = screen.getByRole('navigation', { name: 'Libraries' })
    expect(within(sidebar).getByRole('button', { name: /^Movies/ })).toHaveTextContent('3')
    fireEvent.change(screen.getByLabelText('Filter by name'), { target: { value: 'Movie A' } })
    expect(within(sidebar).getByRole('button', { name: /^Movies/ })).toHaveTextContent('1')
    expect(within(sidebar).getByRole('button', { name: /^TV Shows/ })).toHaveTextContent('0')
  })
})

describe('Browse actions (#207)', () => {
  it('scans the selected titles with their library', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    fireEvent.click(screen.getByLabelText('Select all filtered titles'))
    fireEvent.click(screen.getByRole('button', { name: 'Scan selected now' }))
    await waitFor(() => expect(mockApi.post.mock.calls.filter(([p]: [string]) => p === '/api/scan/title')).toHaveLength(3))
    expect(mockApi.post).toHaveBeenCalledWith('/api/scan/title', { plex_guid: 'g1', now: true, library_id: 'lib1' })
  })

  it('selects every episode of a show from its row', async () => {
    renderAt('/browse?lib=lib2')
    await screen.findByTitle('Open The Show')
    fireEvent.click(screen.getByLabelText('Select The Show'))
    expect(screen.getByText('3 titles selected')).toBeInTheDocument()
  })

  it('syncs a library from Plex only on request', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    fireEvent.click(screen.getByRole('button', { name: /^Sync from Plex/ }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/libraries/lib1/sync'))
  })

  it('retries the failed titles and reports what the server did (#150)', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    fireEvent.click(screen.getByRole('button', { name: /^Retry failed/ }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/rescan', { scope: 'failed', library_id: 'lib1' }))
    expect(await screen.findByText(/2 failed titles queued for another try · 1 ignored cleared/)).toBeInTheDocument()
  })

  it('queues a whole show from its row (#187)', async () => {
    renderAt('/browse?lib=lib2')
    await screen.findByTitle('Open The Show')
    fireEvent.click(screen.getByRole('button', { name: 'Scan whole show tonight' }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/show', {
      show_guid: 'show-1', show_title: 'The Show', season: '', library_id: 'lib2', now: false }))
  })

  it('queues one season now from the season page (#187)', async () => {
    renderAt('/browse?lib=lib2&show=show-1&season=Season%202')
    await screen.findByText('Return')
    fireEvent.click(screen.getByRole('button', { name: 'Scan Season 2 now' }))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/show', {
      show_guid: 'show-1', show_title: 'The Show', season: 'Season 2', library_id: 'lib2', now: true }))
  })

  it('offers the listed episodes or the whole show on right-click (#137)', async () => {
    renderAt('/browse?lib=lib2')
    fireEvent.contextMenu(await screen.findByTitle('Open The Show'))
    fireEvent.click(await screen.findByText('Rescan tonight'))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/titles', { plex_guids: ['e1', 'e2'], now: false }))

    const sidebar = screen.getByRole('navigation', { name: 'Libraries' })
    fireEvent.contextMenu(within(sidebar).getByRole('button', { name: /^TV Shows/ }))
    fireEvent.click(await screen.findByText('Rescan whole library tonight'))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/scan/rescan', { scope: 'whole_library', library_id: 'lib2' }))
  })

  it('asks before deleting a title\'s segments, then deletes them', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    fireEvent.click(screen.getByRole('button', { name: 'Delete all segments for Movie A' }))
    expect(mockApi.delete).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mockApi.delete).toHaveBeenCalledWith('/api/titles/g1/segments'))
  })
})

describe('Browse segments view (#207, #208)', () => {
  it('lists the segments of the matching titles at library level as rows', async () => {
    renderAt('/browse?lib=lib1&view=segments')
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/titles/segments/batch', { guids: ['g1'] }))
    expect(await screen.findByText('2 segments in 1 title')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Play segment/ })).toHaveLength(2)

    fireEvent.change(screen.getByLabelText('Filter by name'), { target: { value: 'Movie B' } })
    expect(await screen.findByText('No segments here.')).toBeInTheDocument()
  })

  it('filters the listed segments by label from the filter panel', async () => {
    renderAt('/browse?lib=lib1&view=segments')
    await screen.findByText('2 segments in 1 title')
    openFilters()
    fireEvent.click(screen.getByRole('button', { name: 'BUTTOCKS', pressed: false }))
    expect(screen.getByText('1 of 2 segments match')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Play segment/ })).toHaveLength(1)
  })

  it('lists segments across libraries at the top level', async () => {
    renderAt('/browse?view=segments')
    expect(await screen.findByText('4 segments in 3 titles')).toBeInTheDocument()
  })

  it('keeps import and export folded away on a title page until asked for', async () => {
    renderAt('/browse?title=g1')
    await screen.findByText('2 segments')
    expect(screen.queryByText('Paste list')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Import or export skip files' }))
    expect(screen.getByText('Paste list')).toBeInTheDocument()
  })
})

describe('Browse markers view (#207, #208)', () => {
  it('shows marker counts in the same rows', async () => {
    renderAt('/browse?lib=lib1&view=markers')
    await screen.findByText('Movie A')
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith('/api/markers/libraries/lib1/titles'))
    expect(screen.getByRole('tab', { name: 'Markers' })).toHaveAttribute('aria-selected', 'true')
    const row = screen.getByTitle('Show segments of Movie A').closest('.group') as HTMLElement
    await waitFor(() => expect(within(row).getByText('2')).toBeInTheDocument())
  })

  it('opens the marker editor on a title page', async () => {
    renderAt('/browse?title=g1&view=markers')
    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith('/api/markers/titles/1'))
  })
})

describe('Browse live updates (#89)', () => {
  it('reloads only the library of the title a finished scan names', async () => {
    renderAt('/browse?lib=lib1')
    await screen.findByText('Movie A')
    const before = [titleCalls('lib1'), titleCalls('lib2')]
    await act(async () => {
      emitLive('scan_finished', { guid: 'g1' })
      await vi.advanceTimersByTimeAsync(600)
    })
    expect([titleCalls('lib1'), titleCalls('lib2')]).toEqual([before[0] + 1, before[1]])
  })

  it('refetches a title\'s segments when a scan changes its count (#90)', async () => {
    renderAt('/browse?lib=lib1&view=segments')
    await screen.findByText('2 segments in 1 title')
    const batches = () => mockApi.post.mock.calls.filter(([p]: [string]) => p === '/api/titles/segments/batch').length
    const before = batches()
    movieList = movies.map(m => (m.plex_guid === 'g1' ? { ...m, segment_count: 3 } : m))
    await act(async () => {
      emitLive('segments_changed', { guid: 'g1' })
      await vi.advanceTimersByTimeAsync(600)
    })
    await waitFor(() => expect(batches()).toBe(before + 1))
  })

  it('shows running scans as one line of title and percent (#110)', async () => {
    scanner = { ...scannerIdle, active_scans: [{ guid: 'g2', title: 'Movie B', progress: 0.42, status: 'scanning' }] } as any
    renderAt('/browse?lib=lib1')
    expect(await screen.findByText('42%')).toBeInTheDocument()
  })
})
