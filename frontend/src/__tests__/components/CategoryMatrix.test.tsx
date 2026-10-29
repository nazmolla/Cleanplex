import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import CategoryMatrix from '../../components/CategoryMatrix'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
}

const defaults = {
  username: 'alice',
  uses_defaults: true,
  categories: [
    { category: 'nudity', level: 3, action: '', has_segments: true },
    { category: 'violence', level: 3, action: '', has_segments: false },
  ],
}

const customised = {
  username: 'alice',
  uses_defaults: false,
  categories: [
    { category: 'nudity', level: 2, action: '', has_segments: true },
    { category: 'language', level: 3, action: 'mute', has_segments: true },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.put.mockResolvedValue({ ok: true })
  mockApi.delete.mockResolvedValue({ ok: true, removed: 1 })
})

describe('CategoryMatrix', () => {
  it('spells out which severities each level skips (#126)', async () => {
    mockApi.get.mockResolvedValue(defaults)
    render(<CategoryMatrix username="alice" enabled={true} />)
    await screen.findByText('What does each level skip?')

    const row = (label: RegExp) => screen.getByText(label).closest('tr') as HTMLElement
    const cells = (tr: HTMLElement) => Array.from(tr.querySelectorAll('td')).slice(1).map(td => td.textContent)
    expect(cells(row(/^0 · Off/))).toEqual(['allowed', 'allowed', 'allowed'])
    expect(cells(row(/^1 · Severe only/))).toEqual(['skipped', 'allowed', 'allowed'])
    expect(cells(row(/^2 · Moderate and up/))).toEqual(['skipped', 'skipped', 'allowed'])
    expect(cells(row(/^3 · Everything/))).toEqual(['skipped', 'skipped', 'skipped'])
  })

  it('says exactly what the nudity level skips, following the level (#126)', async () => {
    mockApi.get.mockResolvedValue(defaults)
    const { rerender } = render(<CategoryMatrix username="alice" enabled={true} />)
    expect(await screen.findByText('Skips everything detected, including covered body parts.')).toBeInTheDocument()

    mockApi.get.mockResolvedValue(customised)
    rerender(<CategoryMatrix username="bob" enabled={true} />)
    expect(await screen.findByText('Skips exposed nudity: genitalia, anus, breasts and buttocks.')).toBeInTheDocument()
    expect(screen.queryByText(/including covered body parts/)).not.toBeInTheDocument()
  })

  it('renders defaults rather than blank controls for a new user', async () => {
    mockApi.get.mockResolvedValue(defaults)

    render(<CategoryMatrix username="alice" enabled={true} />)

    await waitFor(() => {
      expect(screen.getByText(/Using defaults/)).toBeInTheDocument()
    })
    const sliders = screen.getAllByRole('slider') as HTMLInputElement[]
    expect(sliders.every(s => s.value === '3')).toBe(true)
  })

  it('marks categories that have no segments yet', async () => {
    mockApi.get.mockResolvedValue(defaults)

    render(<CategoryMatrix username="alice" enabled={true} />)

    await waitFor(() => {
      expect(screen.getByText('No segments yet')).toBeInTheDocument()
    })
  })

  it('persists a level change', async () => {
    mockApi.get.mockResolvedValue(customised)

    render(<CategoryMatrix username="alice" enabled={true} />)
    await waitFor(() => expect(screen.getAllByRole('slider').length).toBe(2))

    fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '1' } })

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith('/api/users/alice/categories/nudity', {
        level: 1,
        action: '',
      })
    })
  })

  it('persists an action override', async () => {
    mockApi.get.mockResolvedValue(customised)

    render(<CategoryMatrix username="alice" enabled={true} />)
    await waitFor(() => expect(screen.getAllByTitle('Always mute').length).toBe(2))

    fireEvent.click(screen.getAllByTitle('Always mute')[0])

    await waitFor(() => {
      expect(mockApi.put).toHaveBeenCalledWith('/api/users/alice/categories/nudity', {
        level: 2,
        action: 'mute',
      })
    })
  })

  it('offers a reset only once preferences are customised', async () => {
    mockApi.get.mockResolvedValue(defaults)
    const { rerender } = render(<CategoryMatrix username="alice" enabled={true} />)
    await waitFor(() => expect(screen.getByText(/Using defaults/)).toBeInTheDocument())
    expect(screen.queryByText('Reset')).not.toBeInTheDocument()

    mockApi.get.mockResolvedValue(customised)
    rerender(<CategoryMatrix username="bob" enabled={true} />)

    await waitFor(() => expect(screen.getByText('Reset')).toBeInTheDocument())
  })

  it('resets preferences back to defaults', async () => {
    mockApi.get.mockResolvedValueOnce(customised).mockResolvedValueOnce(defaults)

    render(<CategoryMatrix username="alice" enabled={true} />)
    await waitFor(() => expect(screen.getByText('Reset')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Reset'))

    await waitFor(() => {
      expect(mockApi.delete).toHaveBeenCalledWith('/api/users/alice/categories')
      expect(screen.getByText(/Using defaults/)).toBeInTheDocument()
    })
  })

  it('shows the matrix as inert when filtering is off for the user', async () => {
    mockApi.get.mockResolvedValue(defaults)

    const { container } = render(<CategoryMatrix username="alice" enabled={false} />)

    await waitFor(() => expect(screen.getByText(/Filtering is off/)).toBeInTheDocument())
    expect(container.querySelector('.pointer-events-none')).not.toBeNull()
  })

  it('surfaces a save failure instead of showing a change that did not persist', async () => {
    mockApi.get.mockResolvedValue(customised)
    mockApi.put.mockRejectedValue(new Error('500'))

    render(<CategoryMatrix username="alice" enabled={true} />)
    await waitFor(() => expect(screen.getAllByRole('slider').length).toBe(2))

    fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '0' } })

    await waitFor(() => {
      expect(screen.getByText('Could not save that change.')).toBeInTheDocument()
    })
  })

  it('aborts the in-flight request when the user changes', async () => {
    mockApi.get.mockResolvedValue(defaults)

    const { rerender } = render(<CategoryMatrix username="alice" enabled={true} />)
    rerender(<CategoryMatrix username="bob" enabled={true} />)

    await waitFor(() => {
      const lastCall = mockApi.get.mock.calls[mockApi.get.mock.calls.length - 1]
      expect(lastCall[0]).toBe('/api/users/bob/categories')
      expect(lastCall[1].signal).toBeInstanceOf(AbortSignal)
    })
  })
})
