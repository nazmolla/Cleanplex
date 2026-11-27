import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Setup, { applyPreset } from '../../pages/Setup'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('../../components/HayaaPanel', () => ({ default: () => <div>hayaa panel</div> }))

import { api } from '../../api/client'
const mockApi = api as unknown as Record<'get' | 'post' | 'put' | 'delete', ReturnType<typeof vi.fn>>

function plexDetect(result: Record<string, unknown>) {
  mockApi.get.mockImplementation((path: string) => Promise.resolve(
    path === '/api/setup/status' ? { plex_connected: false, server_name: '' } : {}))
  mockApi.post.mockImplementation((path: string) => Promise.resolve(path === '/api/setup/detect' ? result : { ok: true }))
}

async function toPlexStep() {
  render(<MemoryRouter><Setup /></MemoryRouter>)
  fireEvent.click(screen.getByText('Get started'))
}

describe('Setup wizard', () => {
  beforeEach(() => { Object.values(mockApi).forEach(m => m.mockReset()) })

  it('connects to Plex found on this computer', async () => {
    plexDetect({ ok: true, server_name: 'Home' })
    await toPlexStep()
    expect(await screen.findByText('Connected to your Plex server "Home".')).toBeTruthy()
    expect((screen.getByText('Next') as HTMLButtonElement).disabled).toBe(false)
  })

  it('says Plex is not on this computer and blocks Next', async () => {
    plexDetect({ ok: false, reason: 'not_found' })
    await toPlexStep()
    expect(await screen.findByText(/Plex Media Server was not found on this computer/)).toBeTruthy()
    expect(screen.queryByText('Sign in with Plex')).toBeNull()
    expect((screen.getByText('Next') as HTMLButtonElement).disabled).toBe(true)
  })

  it('offers Sign in with Plex when Plex runs here but the token does not work', async () => {
    plexDetect({ ok: false, reason: 'sign_in_needed' })
    await toPlexStep()
    expect(await screen.findByText(/could not connect to it automatically/)).toBeTruthy()
    expect(screen.getByText('Sign in with Plex')).toBeTruthy()
  })

  it('refuses a Plex server on another computer without offering sign-in', async () => {
    plexDetect({ ok: false, reason: 'not_on_this_machine' })
    await toPlexStep()
    expect(await screen.findByText(/Your Plex server is not on this computer/)).toBeTruthy()
    expect(screen.queryByText('Sign in with Plex')).toBeNull()
  })

  it('applies a preset as explicit category levels', async () => {
    mockApi.delete.mockResolvedValue({ ok: true })
    mockApi.put.mockResolvedValue({ ok: true })
    await applyPreset('kid', 'nudity')
    expect(mockApi.delete).toHaveBeenCalledWith('/api/users/kid/categories')
    expect(mockApi.put).toHaveBeenCalledWith('/api/users/kid/categories/nudity', { level: 4, action: '' })
    expect(mockApi.put).toHaveBeenCalledWith('/api/users/kid/categories/language', { level: 0, action: '' })
  })

  it('"Everything" just resets to the default levels', async () => {
    mockApi.delete.mockResolvedValue({ ok: true })
    await applyPreset('kid', 'everything')
    expect(mockApi.delete).toHaveBeenCalledTimes(1)
    expect(mockApi.put).not.toHaveBeenCalled()
  })
})
