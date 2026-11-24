import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import SkipFileImport from '../../components/SkipFileImport'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    upload: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  upload: ReturnType<typeof vi.fn>
}

beforeEach(() => vi.clearAllMocks())

describe('SkipFileImport', () => {
  it('uploads a chosen skip file with the target guid', async () => {
    mockApi.upload.mockResolvedValue({ imported: 2, source: 'skp', title: 'Movie', warning: null })

    const { container } = render(<SkipFileImport plexGuid="guid-1" />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['0:00:10 --> 0:00:20\nnudity 3\n'], 'movie.skp', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => {
      expect(mockApi.upload).toHaveBeenCalledWith('/api/segments/import', expect.any(FormData))
      const form = mockApi.upload.mock.calls[0][1] as FormData
      expect(form.get('plex_guid')).toBe('guid-1')
    })
    expect(await screen.findByText(/Imported 2 segments from skp/)).toBeInTheDocument()
  })

  it('shows the parser message when a file cannot be read', async () => {
    mockApi.upload.mockRejectedValue(new Error('No skip cues found.'))

    const { container } = render(<SkipFileImport plexGuid="guid-1" />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement

    fireEvent.change(input, {
      target: { files: [new File(['junk'], 'movie.skp', { type: 'text/plain' })] },
    })

    expect(await screen.findByText('No skip cues found.')).toBeInTheDocument()
  })

  it('surfaces a runtime mismatch warning alongside the success', async () => {
    mockApi.upload.mockResolvedValue({
      imported: 3,
      source: 'skp',
      title: 'Movie',
      warning: 'The timings were probably authored against a different release.',
    })

    const { container } = render(<SkipFileImport plexGuid="guid-1" />)
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, {
      target: { files: [new File(['x'], 'movie.skp')] },
    })

    expect(await screen.findByText(/different release/)).toBeInTheDocument()
  })

  it('imports a pasted list', async () => {
    mockApi.post.mockResolvedValue({ imported: 1, source: 'paste', title: 'Movie', warning: null })

    render(<SkipFileImport plexGuid="guid-1" />)
    fireEvent.click(screen.getByText('Paste list'))
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: '00:12:30 - 00:13:05 nudity' },
    })
    fireEvent.click(screen.getByText('Import list'))

    await waitFor(() => {
      expect(mockApi.post).toHaveBeenCalledWith('/api/segments/import/paste', {
        plex_guid: 'guid-1',
        text: '00:12:30 - 00:13:05 nudity',
        replace: false,
      })
    })
  })

  it('cannot submit an empty pasted list', () => {
    render(<SkipFileImport plexGuid="guid-1" />)
    fireEvent.click(screen.getByText('Paste list'))

    expect(screen.getByText('Import list')).toBeDisabled()
  })

  it('passes the replace flag when asked to overwrite', async () => {
    mockApi.upload.mockResolvedValue({ imported: 1, source: 'edl', title: 'M', warning: null })

    const { container } = render(<SkipFileImport plexGuid="guid-1" />)
    fireEvent.click(screen.getByLabelText('Replace existing'))
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, {
      target: { files: [new File(['10 20 0'], 'movie.edl')] },
    })

    await waitFor(() => {
      const form = mockApi.upload.mock.calls[0][1] as FormData
      expect(form.get('replace')).toBe('true')
    })
  })

  it('offers no export of segments', () => {
    render(<SkipFileImport plexGuid="guid-1" />)
    expect(screen.queryByText(/Export/)).toBeNull()
  })

  it('notifies the parent so the segment list can refresh', async () => {
    mockApi.upload.mockResolvedValue({ imported: 4, source: 'skp', title: 'M', warning: null })
    const onImported = vi.fn()

    const { container } = render(<SkipFileImport plexGuid="guid-1" onImported={onImported} />)
    fireEvent.change(container.querySelector('input[type="file"]') as HTMLInputElement, {
      target: { files: [new File(['x'], 'movie.skp')] },
    })

    await waitFor(() => expect(onImported).toHaveBeenCalledWith(4))
  })
})
