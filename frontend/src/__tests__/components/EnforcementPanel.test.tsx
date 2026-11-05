import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import EnforcementPanel from '../../components/EnforcementPanel'

vi.mock('../../api/client', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
import { api } from '../../api/client'
const mockApi = api as { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }

const status = {
  enabled: false, mute_mode: 'leave', backstop: true, proxied_last_day: 3,
  plex: { relay: '1', dlna: '1', transcode_limit: '2' }, recommendations: { relay: false, dlna: false },
}

beforeEach(() => {
  mockApi.get.mockResolvedValue(status)
  mockApi.post.mockResolvedValue({ ...status, plex: { ...status.plex, relay: '0' } })
})

describe('EnforcementPanel', () => {
  it('shows whether the front door is installed and the Plex settings', async () => {
    render(<EnforcementPanel />)
    await waitFor(() => expect(screen.getByText(/Front door not installed/)).toBeInTheDocument())
    expect(screen.getByText(/Relay: On/)).toBeInTheDocument()
    expect(screen.getByText(/Streams edited in the last day: 3/)).toBeInTheDocument()
  })

  it('turns Relay off through the Plex settings endpoint', async () => {
    render(<EnforcementPanel />)
    await waitFor(() => screen.getByText(/Relay: On/))
    fireEvent.click(screen.getAllByRole('button', { name: 'Turn off' })[0])
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/enforcement/plex-settings', { relay: false }))
    await waitFor(() => expect(screen.getByText(/Relay: Off/)).toBeInTheDocument())
  })

  it('changes the mute mode', async () => {
    render(<EnforcementPanel />)
    await waitFor(() => screen.getByText(/Relay: On/))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'cut' } })
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith('/api/enforcement', { mute_mode: 'cut' }))
  })
})

describe('EnforcementPanel with Plex True/False values', () => {
  it('reads capitalised booleans', async () => {
    mockApi.get.mockResolvedValue({ ...status, plex: { relay: 'False', dlna: 'True', transcode_limit: '6' } })
    render(<EnforcementPanel />)
    await waitFor(() => expect(screen.getByText(/Relay: Off/)).toBeInTheDocument())
    expect(screen.getByText(/DLNA: On/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Turn on' })).toBeInTheDocument()
  })
})
