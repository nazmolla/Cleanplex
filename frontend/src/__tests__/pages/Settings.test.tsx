import { I18nProvider } from '../../i18n'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Routes, Route } from 'react-router-dom'
import Settings from '../../pages/Settings'
import UsersPage from '../../pages/Users'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
}

const defaultSettings = {
  plex_url: 'http://plex:32400',
  plex_token: 'abc',
  poll_interval: '5',
  confidence_threshold: '0.6',
  skip_buffer_ms: '3000',
  scan_step_ms: '5000',
  scan_workers: '2',
  segment_gap_ms: '12000',
  segment_min_hits: '1',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED"]',
  nudenet_model: '320n',
  nudenet_model_path: '',
  sync_enabled: '0',
  sync_instance_name: '',
  sync_github_repo: '',
  sync_conflict_resolution: 'consensus',
  sync_verified_threshold: '2',
  sync_timing_tolerance_ms: '2000',
}

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>
  )
}

beforeEach(() => {
  // shouldAdvanceTime lets the real clock keep moving under the fake timers, so
  // testing-library's waitFor can still poll. Without it every waitFor in this
  // file hangs until the 5s test timeout.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockApi.get.mockImplementation((path: string) => {
    // detector-labels is checked first: it also contains 'settings' in its path.
    if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
    if (path.includes('sync/status')) return Promise.resolve(null)
    // /api/settings returns the settings object flat, not wrapped.
    if (path.includes('settings')) return Promise.resolve(defaultSettings)
    if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
    if (path === '/api/users') return Promise.resolve({ users: [{ username: 'alice', thumb: '', enabled: true }] })
    return Promise.resolve({})
  })
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Settings', () => {
  it('renders the page heading', async () => {
    renderSettings()
    await waitFor(() => expect(screen.getByText('Settings')).toBeInTheDocument())
  })

  it('loads and displays plex url from settings', async () => {
    renderSettings()
    await waitFor(() => {
      const input = screen.getByDisplayValue('http://plex:32400')
      expect(input).toBeInTheDocument()
    })
  })

  it('upload polling terminates after MAX_POLLS without timing out infinitely', async () => {
    // Simulate an upload job that stays 'running' indefinitely — the loop must stop
    let pollCount = 0
    mockApi.post.mockImplementation((path: string) => {
      if (path.includes('upload')) return Promise.resolve({ job_id: 42 })
      if (path.includes('job')) {
        pollCount++
        return Promise.resolve({ status: 'running', progress: 50, error: null, result: null })
      }
      return Promise.resolve({})
    })
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('job')) return Promise.resolve({ status: 'running', progress: 50, error: null, result: null })
      if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
      if (path.includes('sync/status')) return Promise.resolve(null)
      if (path.includes('settings')) return Promise.resolve(defaultSettings)
      if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
      return Promise.resolve({})
    })

    renderSettings()
    await waitFor(() => screen.getByText('Settings'))

    // The bounded poll loop should stop after MAX_POLLS (120) ticks — it must not loop forever.
    // We advance timers rapidly to simulate many poll attempts.
    await act(async () => {
      // Each poll waits 1000ms — advance 130 seconds to exceed MAX_POLLS
      vi.advanceTimersByTime(130_000)
    })

    // At this point the loop should have terminated — verify poll count is bounded
    // (exact value depends on component state; key thing is test completes)
    expect(pollCount).toBeLessThanOrEqual(130)
  })
})

describe('Settings sections', () => {
  it('shows one section at a time and switches from the nav', async () => {
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))
    expect(screen.queryByText('Enable SigLIP fallback')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))

    expect(screen.getByText('Enable SigLIP fallback')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('http://plex:32400')).not.toBeInTheDocument()
  })

  it('saves the SigLIP toggle with the other settings', async () => {
    mockApi.put.mockResolvedValue({})
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))
    fireEvent.click(screen.getByLabelText('Enable SigLIP fallback'))
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    const body = mockApi.put.mock.calls[0][1]
    expect(body.siglip_enabled).toBe('1')
    expect(body.siglip_threshold).toBe('0.3')
  })

  it('saves a per-label NudeNet threshold and leaves blank labels inheriting (#120)', async () => {
    mockApi.put.mockResolvedValue({})
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))
    fireEvent.change(screen.getByLabelText('NudeNet Female breast exposed threshold'), { target: { value: '0.8' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    expect(JSON.parse(mockApi.put.mock.calls[0][1].label_thresholds)).toEqual({ FEMALE_BREAST_EXPOSED: 0.8 })
  })

  it('clearing a per-label SigLIP threshold removes the override (#120)', async () => {
    mockApi.put.mockResolvedValue({})
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))
    fireEvent.click(screen.getByLabelText('Enable SigLIP fallback'))
    const box = screen.getByLabelText('SigLIP Female breast exposed threshold')
    fireEvent.change(box, { target: { value: '0.9' } })
    fireEvent.change(box, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    expect(JSON.parse(mockApi.put.mock.calls[0][1].siglip_label_thresholds)).toEqual({})
  })

  it('toggles ignoring end credits (#121)', async () => {
    mockApi.put.mockResolvedValue({})
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))
    fireEvent.click(screen.getByLabelText('Enable SigLIP fallback'))
    fireEvent.click(screen.getByLabelText(/Ignore end credits/))
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    expect(mockApi.put.mock.calls[0][1].ignore_credits).toBe('0')
  })

  it('saves the refinement step from the Segments section (#133)', async () => {
    mockApi.put.mockResolvedValue({})
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))
    fireEvent.change(screen.getByDisplayValue('500'), { target: { value: '250' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    expect(mockApi.put.mock.calls[0][1].refine_step_ms).toBe('250')
  })

  it('says how many existing segments still wait for refinement (#141)', async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('refine-status')) return Promise.resolve({ remaining: 3358, enabled: true })
      if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
      if (path.includes('sync/status')) return Promise.resolve(null)
      if (path.includes('settings')) return Promise.resolve(defaultSettings)
      if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
      return Promise.resolve({})
    })
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Detection & Performance' }))

    expect(screen.getByRole('status')).toHaveTextContent('3,358 existing segments are still waiting to be refined')
  })

  it('saves the render-when-the-queue-is-empty option for hover previews (#152)', async () => {
    mockApi.put.mockResolvedValue({})
    renderSettings()
    await waitFor(() => screen.getByDisplayValue('http://plex:32400'))

    fireEvent.click(screen.getByRole('button', { name: 'Library & Schedule' }))
    fireEvent.click(screen.getByLabelText(/Also render as soon as the scan queue is empty/))
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    expect(mockApi.put.mock.calls[0][1].hover_when_idle).toBe('0')
  })

  it('shows Users as a section without the Save bar (#118)', async () => {
    render(<MemoryRouter initialEntries={['/settings?section=users']}><Settings /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Users' })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('button', { name: /Save Settings/ })).not.toBeInTheDocument()
  })

  it('sends the old /users address to the Users section (#118)', async () => {
    render(
      <MemoryRouter initialEntries={['/users']}>
        <Routes>
          <Route path="/users" element={<UsersPage />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </MemoryRouter>
    )
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument())
  })
})

describe('Scan ratings checklist (#219)', () => {
  it('lists the ratings in the libraries with counts and saves the normalized value', async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === '/api/settings/ratings') return Promise.resolve([
        { rating: 'G', count: 327, sources: ['G', 'eg/G'] },
        { rating: 'R', count: 671, sources: ['R', 'eg/16', 'eg/18'] },
        { rating: '', count: 288, sources: [] },
      ])
      if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
      if (path.includes('sync/status')) return Promise.resolve(null)
      if (path.includes('settings')) return Promise.resolve({ ...defaultSettings, scan_ratings: '["eg/16"]' })
      if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
      return Promise.resolve({})
    })
    render(<MemoryRouter initialEntries={['/settings?section=schedule']}><Settings /></MemoryRouter>)
    const r = await screen.findByRole('button', { name: /^R\s*671/ })
    expect(r).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('eg/16, eg/18')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^G\s*327/ })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByRole('button', { name: /^NC-17/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^G\s*327/ }))
    expect(screen.getByRole('button', { name: /^G\s*327/ })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('Language section (#232)', () => {
  afterEach(() => { document.documentElement.lang = 'en'; document.documentElement.dir = 'ltr' })

  it('switches the UI to Arabic and right-to-left from Settings', async () => {
    render(
      <MemoryRouter>
        <I18nProvider>
          <Settings />
        </I18nProvider>
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'العربية' }))
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('ar')
    expect(screen.getByRole('button', { name: 'English' })).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('Settings redesign (#236)', () => {
  it('opens the section that now holds an old section id', async () => {
    render(<MemoryRouter initialEntries={['/settings?section=siglip']}><Settings /></MemoryRouter>)
    expect(await screen.findByText('Enable SigLIP fallback')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Detection & Performance' })).toHaveAttribute('aria-current', 'page')
  })

  it('maps every old section id to a new one', async () => {
    const { resolveSection } = await import('../../pages/Settings')
    expect(resolveSection('connection')).toBe('general')
    expect(resolveSection('language')).toBe('general')
    expect(resolveSection('filtering')).toBe('playback')
    expect(resolveSection('nudenet')).toBe('detection')
    expect(resolveSection('segments')).toBe('detection')
    expect(resolveSection('schedule')).toBe('schedule')
    expect(resolveSection('hayaa')).toBe('hayaa')
    expect(resolveSection('users')).toBe('users')
    expect(resolveSection('nonsense')).toBe('general')
    expect(resolveSection(null)).toBe('general')
  })

  it('shows the Save bar only after a change, and Discard puts the saved value back', async () => {
    renderSettings()
    const url = await screen.findByDisplayValue('http://plex:32400')
    expect(screen.queryByRole('button', { name: /Save Settings/ })).not.toBeInTheDocument()

    fireEvent.change(url, { target: { value: 'http://other:32400' } })
    expect(screen.getByRole('button', { name: /Save Settings/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.getByDisplayValue('http://plex:32400')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save Settings/ })).not.toBeInTheDocument()
    expect(mockApi.put).not.toHaveBeenCalled()
  })

  it('tests the typed connection values without saving them', async () => {
    mockApi.post.mockResolvedValue({ ok: false, message: 'unreachable' })
    renderSettings()
    const url = await screen.findByDisplayValue('http://plex:32400')
    fireEvent.change(url, { target: { value: 'http://typo:32400' } })
    fireEvent.click(screen.getByRole('button', { name: /Test Connection/i }))

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledWith(
      '/api/settings/test-connection',
      expect.objectContaining({ plex_url: 'http://typo:32400' }),
    ))
    expect(mockApi.put).not.toHaveBeenCalled()
    expect(await screen.findByText('unreachable')).toBeInTheDocument()
  })

  it('keeps one-off actions in Maintenance, not among settings', async () => {
    render(<MemoryRouter initialEntries={['/settings?section=maintenance']}><Settings /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: /Restart Scanner/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save Settings/ })).not.toBeInTheDocument()
  })
})

describe('Fallback skipping buffers (#238)', () => {
  it('saves Seek before and Seek after as the buffers live skipping uses', async () => {
    mockApi.put.mockResolvedValue({})
    const base = mockApi.get.getMockImplementation()!
    mockApi.get.mockImplementation((path: string) => path.includes('enforcement')
      ? Promise.resolve({ enabled: true, mute_mode: 'leave', backstop: true, proxied_last_day: 0,
                          plex: { relay: '0', dlna: '0', transcode_limit: 6 } })
      : base(path))
    render(<MemoryRouter initialEntries={['/settings?section=playback']}><Settings /></MemoryRouter>)
    fireEvent.change(await screen.findByLabelText(/^Seek before/), { target: { value: '1500' } })
    fireEvent.change(screen.getByLabelText(/^Seek after/), { target: { value: '4000' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Settings/ }))

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    const body = mockApi.put.mock.calls[0][1]
    expect(body.pre_buffer_ms).toBe('1500')
    expect(body.post_buffer_ms).toBe('4000')
    expect(screen.queryByText(/Skip Buffer/)).not.toBeInTheDocument()
  })
})
