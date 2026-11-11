import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import SegmentPlayer from '../../components/SegmentPlayer'
import { api } from '../../api/client'

describe('SegmentPlayer (#189)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('shows a loading message with elapsed time until the clip can play', () => {
    const { container } = render(<SegmentPlayer segmentId={7} />)
    expect(screen.getByRole('status')).toHaveTextContent('Rendering the preview')
    expect(container.querySelector('video')!.getAttribute('src')).toBe('/api/segments/7/preview')

    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.getByRole('status')).toHaveTextContent('3s')

    fireEvent.canPlay(container.querySelector('video')!)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows the server reason and a Retry button when the clip fails', async () => {
    vi.spyOn(api, 'mediaProblem').mockResolvedValue({ ok: false, detail: 'Source file not found for this segment' })
    const { container } = render(<SegmentPlayer segmentId={7} />)

    fireEvent.error(container.querySelector('video')!)

    expect(await screen.findByRole('alert')).toHaveTextContent('Source file not found for this segment')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('falls back to a generic message when the server cannot be reached', async () => {
    vi.spyOn(api, 'mediaProblem').mockRejectedValue(new Error('network'))
    const { container } = render(<SegmentPlayer segmentId={7} />)

    fireEvent.error(container.querySelector('video')!)

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not reach the server'))
  })

  it('retries with a fresh URL and a new video element', async () => {
    vi.spyOn(api, 'mediaProblem').mockResolvedValue({ ok: false, detail: 'Could not render a preview of this segment' })
    const { container } = render(<SegmentPlayer segmentId={7} />)
    fireEvent.error(container.querySelector('video')!)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(container.querySelector('video')!.getAttribute('src')).toBe('/api/segments/7/preview?retry=1')
  })

  it('gives up with a message when the render never finishes', () => {
    render(<SegmentPlayer segmentId={7} />)

    act(() => { vi.advanceTimersByTime(301_000) })

    expect(screen.getByRole('alert')).toHaveTextContent('taking too long')
  })
})
