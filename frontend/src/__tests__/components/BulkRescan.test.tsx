import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import BulkRescan from '../../components/BulkRescan'

vi.mock('../../api/client', () => ({ api: { post: vi.fn() } }))

import { api } from '../../api/client'
const post = api.post as ReturnType<typeof vi.fn>

const LIBS = [{ id: '1', title: 'Movies' }, { id: '2', title: 'TV' }]
const counts = (queued: number, skipped = 0) => ({ ok: true, matched: queued + skipped, queued, skipped_ineligible: skipped })

beforeEach(() => {
  vi.clearAllMocks()
  post.mockImplementation((_path: string, body: any) => Promise.resolve(counts(body.dry_run ? 12 : 12, 3)))
})

describe('BulkRescan (#138)', () => {
  it('counts what the default scope would queue, without queueing it', async () => {
    render(<BulkRescan libraries={LIBS} />)

    expect(await screen.findByText(/12 titles will be queued/)).toBeInTheDocument()
    expect(screen.getByText(/3 skipped by your rating filters/)).toBeInTheDocument()
    expect(post).toHaveBeenCalledWith('/api/scan/rescan', { scope: 'outdated', library_id: null, before: null, dry_run: true })
    expect(post.mock.calls.every(c => c[1].dry_run)).toBe(true)
  })

  it('recounts for another scope and library', async () => {
    render(<BulkRescan libraries={LIBS} />)
    await screen.findByText(/12 titles/)

    fireEvent.click(screen.getByLabelText(/Titles that have segments/))
    fireEvent.change(screen.getByLabelText('Library'), { target: { value: '2' } })

    await waitFor(() => expect(post).toHaveBeenLastCalledWith(
      '/api/scan/rescan', { scope: 'with_segments', library_id: '2', before: null, dry_run: true }))
  })

  it('needs a date for the scanned-before scope and counts once it has one', async () => {
    render(<BulkRescan libraries={LIBS} />)
    await screen.findByText(/12 titles/)
    post.mockClear()

    fireEvent.click(screen.getByLabelText(/Scanned before a date/))
    expect(screen.getByText('Pick a date.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Queue rescan' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Before'), { target: { value: '2026-10-01' } })
    await waitFor(() => expect(post).toHaveBeenCalledWith(
      '/api/scan/rescan', { scope: 'scanned_before', library_id: null, before: '2026-10-01', dry_run: true }))
  })

  it('asks to confirm, then queues for real and reports it', async () => {
    render(<BulkRescan libraries={LIBS} />)
    await screen.findByText(/12 titles/)

    fireEvent.click(screen.getByRole('button', { name: 'Queue rescan' }))
    expect(post.mock.calls.some(c => !c[1].dry_run)).toBe(false)  // nothing queued yet
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByText('12 titles queued for the scan window.')).toBeInTheDocument()
    expect(post).toHaveBeenLastCalledWith('/api/scan/rescan', { scope: 'outdated', library_id: null, before: null, dry_run: false })
  })

  it('cancelling leaves everything unqueued', async () => {
    render(<BulkRescan libraries={LIBS} />)
    await screen.findByText(/12 titles/)

    fireEvent.click(screen.getByRole('button', { name: 'Queue rescan' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(post.mock.calls.every(c => c[1].dry_run)).toBe(true)
    expect(screen.getByRole('button', { name: 'Queue rescan' })).toBeInTheDocument()
  })

  it('cannot queue when nothing matches', async () => {
    post.mockResolvedValue(counts(0))
    render(<BulkRescan libraries={LIBS} />)

    expect(await screen.findByText(/0 titles will be queued/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Queue rescan' })).toBeDisabled()
  })

  it('ignores a slow count that arrives after a newer one', async () => {
    let releaseFirst: (v: unknown) => void = () => {}
    post.mockReset()
    post.mockImplementationOnce(() => new Promise(r => { releaseFirst = r }))
    post.mockImplementation(() => Promise.resolve(counts(5)))
    render(<BulkRescan libraries={LIBS} />)

    fireEvent.click(screen.getByLabelText(/Every scanned title/))
    expect(await screen.findByText(/5 titles will be queued/)).toBeInTheDocument()
    releaseFirst(counts(999))
    await new Promise(r => setTimeout(r, 10))

    expect(screen.getByText(/5 titles will be queued/)).toBeInTheDocument()
    expect(screen.queryByText(/999/)).not.toBeInTheDocument()
  })

  it('reports a failed queue request', async () => {
    render(<BulkRescan libraries={LIBS} />)
    await screen.findByText(/12 titles/)
    post.mockRejectedValueOnce(new Error('503 scanner down'))

    fireEvent.click(screen.getByRole('button', { name: 'Queue rescan' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByText(/Could not queue the rescan: 503 scanner down/)).toBeInTheDocument()
  })
})
