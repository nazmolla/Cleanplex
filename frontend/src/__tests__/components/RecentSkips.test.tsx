import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import RecentSkips from '../../components/RecentSkips'
import { emitLive } from '../../test/fakeEventSource'

vi.mock('../../api/client', () => ({ api: { get: vi.fn() } }))

import { api } from '../../api/client'
const mockGet = api.get as ReturnType<typeof vi.fn>

function event(id: number, title: string, extra: object = {}) {
  return {
    id,
    title,
    username: 'alice',
    client_title: 'Apple TV',
    category: 'nudity',
    action: 'skip',
    position_ms: 0,
    success: 1,
    created_at: '2026-10-02 10:00:00',
    ...extra,
  }
}

beforeEach(() => {
  mockGet.mockResolvedValue({ events: [event(1, 'Movie A'), event(2, 'Movie B', { action: 'mute' })], total: 2 })
})

afterEach(() => vi.clearAllMocks())

describe('RecentSkips', () => {
  it('lists the returned events with the total, for the last 7 days by default (#99)', async () => {
    render(<RecentSkips />)

    await waitFor(() => expect(screen.getByText('Movie A')).toBeInTheDocument())
    expect(screen.getByText('Movie B')).toBeInTheDocument()
    expect(screen.getByText('(2)')).toBeInTheDocument()
    const day = (offset: number) => {
      const d = new Date()
      d.setDate(d.getDate() + offset)
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    }
    expect((screen.getByLabelText('From date') as HTMLInputElement).value).toBe(day(-6))
    expect((screen.getByLabelText('To date') as HTMLInputElement).value).toBe(day(0))
    const params = new URLSearchParams((mockGet.mock.calls[0][0] as string).split('?')[1])
    expect(params.get('start')).toBe(new Date(`${day(-6)}T00:00:00`).toISOString())
    expect(params.get('end')).toBe(new Date(`${day(1)}T00:00:00`).toISOString())
  })

  it('Clear lists all history with no date bounds', async () => {
    render(<RecentSkips />)
    await waitFor(() => expect(mockGet).toHaveBeenCalled())

    fireEvent.click(screen.getByText('Clear'))

    await waitFor(() => {
      const url = mockGet.mock.calls.at(-1)![0] as string
      expect(url).not.toContain('start=')
      expect(url).not.toContain('end=')
    })
  })

  it('sends the chosen local days as a start-inclusive, end-exclusive window', async () => {
    render(<RecentSkips />)
    await waitFor(() => expect(mockGet).toHaveBeenCalled())

    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-10-01' } })
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2026-10-03' } })

    await waitFor(() => {
      const url = mockGet.mock.calls.at(-1)![0] as string
      const params = new URLSearchParams(url.split('?')[1])
      expect(params.get('start')).toBe(new Date('2026-10-01T00:00:00').toISOString())
      expect(params.get('end')).toBe(new Date('2026-10-04T00:00:00').toISOString())
    })
  })

  it('pages through history beyond the first page', async () => {
    mockGet.mockResolvedValue({ events: [event(1, 'Movie A')], total: 60 })
    render(<RecentSkips />)

    await waitFor(() => expect(screen.getByText('Page 1 of 3')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Next page'))

    await waitFor(() => expect(mockGet.mock.calls.at(-1)![0]).toContain('offset=25'))
  })

  it('shows a range-specific empty state', async () => {
    mockGet.mockResolvedValue({ events: [], total: 0 })
    render(<RecentSkips />)
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-10-01' } })

    await waitFor(() => expect(screen.getByText('No skips in this date range')).toBeInTheDocument())
  })

  it('reloads when the backend records a skip (#89)', async () => {
    render(<RecentSkips />)
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    emitLive('skip_recorded', { guid: 'g' })

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2))
  })
})
