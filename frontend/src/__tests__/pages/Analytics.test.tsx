import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Analytics from '../../pages/Analytics'
import { emitLive } from '../../test/fakeEventSource'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
}

const seg = (id: number, title: string, extra: Record<string, unknown> = {}) => ({
  id, plex_guid: `g${id}`, title, start_ms: 60_000, end_ms: 65_000, confidence: 0.8,
  labels: 'FEMALE_BREAST_EXPOSED', has_thumbnail: false, thumbnail_url: '', hover_url: '',
  poster_url: '', content_rating: 'R', media_type: 'movie', rating_key: '1', source: 'scanner',
  detector: 'nudenet-320n', ...extra,
})

let segmentsResponse: () => Promise<unknown>

function renderAt(url = '/analytics') {
  return render(<MemoryRouter initialEntries={[url]}><Analytics /></MemoryRouter>)
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  segmentsResponse = () => Promise.resolve({ segments: [seg(1, 'Movie One'), seg(2, 'Movie Two')], total: 2 })
  mockApi.get.mockImplementation((path: string) => {
    if (path === '/api/analytics/labels') return Promise.resolve({ labels: [{ label: 'FEMALE_BREAST_EXPOSED', count: 2 }] })
    if (path.startsWith('/api/analytics/segments')) return segmentsResponse()
    if (path.startsWith('/api/analytics/false-positives')) {
      return Promise.resolve({ segments: [seg(9, 'Wrong One', { false_positive: true, marked_at: '2026-10-27 10:00:00' })], total: 1 })
    }
    if (path.startsWith('/api/analytics/detection-accuracy')) return Promise.resolve({ rows: [], marked: 0 })
    return Promise.resolve({})
  })
  mockApi.post.mockResolvedValue({ ok: true })
  // This jsdom setup has no localStorage (opaque origin); give each test a fresh one.
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
    removeItem: (k: string) => { store.delete(k) },
    clear: () => store.clear(),
  })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

async function openLabel() {
  renderAt()
  // The label is both a chart bar and a filter chip; the chip carries the count.
  const chip = await screen.findByRole('button', { name: /Breast\s*2/i })
  fireEvent.click(chip)
  await screen.findByText('Movie One')
}

describe('Analytics', () => {
  it('shows when in the film each thumbnail was taken (#133)', async () => {
    segmentsResponse = () => Promise.resolve({
      segments: [seg(1, 'Movie One', { thumbnail_ms: 2_892_000, has_thumbnail: true, thumbnail_url: '/t/1' }), seg(2, 'Movie Two')],
      total: 2,
    })
    await openLabel()
    expect(screen.getByText('frame 0:48:12')).toBeInTheDocument()
    // A segment scanned before the time was recorded shows none.
    expect(screen.getAllByText(/^frame /)).toHaveLength(1)
  })

  it('keeps the list on screen through the live refresh after a false-positive mark (#116)', async () => {
    await openLabel()

    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: 'Mark as false positive' })[0])
    })
    await waitFor(() => expect(screen.queryByText('Movie One')).not.toBeInTheDocument())

    // The refetch the mark triggers is still in flight: the remaining row must stay.
    segmentsResponse = () => new Promise(() => {})
    await act(async () => {
      emitLive('segments_changed', { guid: 'g1' })
      await vi.advanceTimersByTimeAsync(2500)
    })
    expect(screen.getByText('Movie Two')).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('opens the tab named in the URL and switches tabs (#117)', async () => {
    renderAt('/analytics?tab=accuracy')
    expect(await screen.findByRole('tab', { name: 'Accuracy' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByText('Segments by label')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Detections' }))
    expect(await screen.findByText('Segments by label')).toBeInTheDocument()
  })

  it('lists false positives and drops one when the mark is undone (#117)', async () => {
    renderAt('/analytics?tab=false-positives')
    expect(await screen.findByText('Wrong One')).toBeInTheDocument()
    expect(screen.getByText(/Marked/)).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Undo false positive' }))
    })

    await waitFor(() => expect(screen.queryByText('Wrong One')).not.toBeInTheDocument())
    expect(mockApi.post).toHaveBeenCalledWith('/api/segments/9/false-positive', { false_positive: false })
  })

  it('still toggles a section when storage is unavailable (#117)', async () => {
    vi.stubGlobal('localStorage', undefined)
    renderAt('/analytics?tab=accuracy')
    const header = await screen.findByRole('button', { name: /Detection accuracy/ })
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'false')
  })

  it('remembers a collapsed section across reloads (#117)', async () => {
    const { unmount } = renderAt('/analytics?tab=accuracy')
    const header = await screen.findByRole('button', { name: /Detection accuracy/ })
    expect(header).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'false')
    unmount()

    renderAt('/analytics?tab=accuracy')
    expect(await screen.findByRole('button', { name: /Detection accuracy/ })).toHaveAttribute('aria-expanded', 'false')
  })

  const lastUrl = (prefix: string) =>
    mockApi.get.mock.calls.map(c => c[0] as string).filter(u => u.startsWith(prefix)).at(-1) as string

  it('sorts the label list by score or date added and flips direction (#128)', async () => {
    await openLabel()
    expect(lastUrl('/api/analytics/segments')).toContain('sort=score&order=desc')

    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'added' } })
    await waitFor(() => expect(lastUrl('/api/analytics/segments')).toContain('sort=added&order=desc'))

    fireEvent.click(screen.getByRole('button', { name: 'Sort descending' }))
    await waitFor(() => expect(lastUrl('/api/analytics/segments')).toContain('sort=added&order=asc'))
    expect(lastUrl('/api/analytics/segments')).toContain('offset=0')
  })

  it('sorts false positives, defaulting to newest mark first (#128)', async () => {
    renderAt('/analytics?tab=false-positives')
    await screen.findByText('Wrong One')
    expect(lastUrl('/api/analytics/false-positives')).toContain('sort=marked&order=desc')

    fireEvent.change(screen.getByLabelText('Sort by'), { target: { value: 'score' } })
    await waitFor(() => expect(lastUrl('/api/analytics/false-positives')).toContain('sort=score&order=desc'))
  })
})
