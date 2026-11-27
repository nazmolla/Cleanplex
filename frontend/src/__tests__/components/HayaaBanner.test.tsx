import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import HayaaBanner, { HAYAA_BANNER_KEY } from '../../components/HayaaBanner'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}))

import { api } from '../../api/client'
const mockApi = api as unknown as { get: ReturnType<typeof vi.fn> }

const at = (path = '/') => render(<MemoryRouter initialEntries={[path]}><HayaaBanner /></MemoryRouter>)

describe('HayaaBanner', () => {
  beforeEach(() => {
    // This jsdom setup has no localStorage (opaque origin); give each test a fresh one.
    const store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v) },
      removeItem: (k: string) => { store.delete(k) },
      clear: () => store.clear(),
    })
    mockApi.get.mockReset()
  })

  it('invites an instance that is not signed in', async () => {
    mockApi.get.mockResolvedValue({ linked: false })
    at()
    expect(await screen.findByText('New: Hayaa community sync')).toBeTruthy()
  })

  it('stays hidden once signed in', async () => {
    mockApi.get.mockResolvedValue({ linked: true })
    at()
    await Promise.resolve(); await Promise.resolve()
    expect(screen.queryByText('New: Hayaa community sync')).toBeNull()
  })

  it('"Not now" hides it for good', async () => {
    mockApi.get.mockResolvedValue({ linked: false })
    at()
    fireEvent.click(await screen.findByText('Not now'))
    expect(screen.queryByText('New: Hayaa community sync')).toBeNull()
    expect(localStorage.getItem(HAYAA_BANNER_KEY)).toBe('1')
  })

  it('does not ask again after dismissal', () => {
    localStorage.setItem(HAYAA_BANNER_KEY, '1')
    at()
    expect(mockApi.get).not.toHaveBeenCalled()
  })

  it('is not shown on the Settings page', async () => {
    mockApi.get.mockResolvedValue({ linked: false })
    at('/settings')
    await Promise.resolve(); await Promise.resolve()
    expect(screen.queryByText('New: Hayaa community sync')).toBeNull()
  })
})
