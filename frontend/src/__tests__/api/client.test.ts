import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../../api/client'

// Replace the global fetch with a vi mock before each test
const mockFetch = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  vi.restoreAllMocks()
})

function mockOk(body: unknown) {
  mockFetch.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  })
}

function mockError(status: number, text: string) {
  mockFetch.mockResolvedValue({
    ok: false,
    status,
    text: () => Promise.resolve(text),
  })
}

describe('api.get', () => {
  it('calls fetch with GET method and the correct URL', async () => {
    mockOk({ sessions: [] })
    await api.get('/api/sessions')
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/sessions',
      expect.objectContaining({ method: 'GET' })
    )
  })

  it('returns parsed JSON on success', async () => {
    const data = { sessions: [{ id: 1 }] }
    mockOk(data)
    const result = await api.get('/api/sessions')
    expect(result).toEqual(data)
  })

  it('throws on non-ok response', async () => {
    mockError(404, 'Not found')
    await expect(api.get('/api/missing')).rejects.toThrow('404')
  })

  it('forwards AbortSignal to fetch', async () => {
    mockOk({})
    const controller = new AbortController()
    await api.get('/api/sessions', { signal: controller.signal })
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/sessions',
      expect.objectContaining({ signal: controller.signal })
    )
  })

  it('aborted request causes fetch to reject', async () => {
    const controller = new AbortController()
    mockFetch.mockRejectedValue(new DOMException('Aborted', 'AbortError'))
    controller.abort()
    await expect(api.get('/api/sessions', { signal: controller.signal })).rejects.toThrow('Aborted')
  })
})

describe('api.post', () => {
  it('calls fetch with POST method and JSON body', async () => {
    mockOk({ ok: true })
    await api.post('/api/sessions/s1/skip', { foo: 'bar' })
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/sessions/s1/skip',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ foo: 'bar' }),
        headers: { 'Content-Type': 'application/json' },
      })
    )
  })

  it('calls fetch with POST and no body when body is omitted', async () => {
    mockOk({ ok: true })
    await api.post('/api/scan/pause')
    const [, init] = mockFetch.mock.calls[0]
    expect(init.body).toBeUndefined()
  })
})

describe('api.put', () => {
  it('calls fetch with PUT method', async () => {
    mockOk({ ok: true })
    await api.put('/api/settings', { plex_url: 'http://localhost' })
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/settings',
      expect.objectContaining({ method: 'PUT' })
    )
  })
})

describe('api.delete', () => {
  it('calls fetch with DELETE method', async () => {
    mockOk({ ok: true })
    await api.delete('/api/segments/1')
    expect(mockFetch).toHaveBeenCalledWith(
      '/api/segments/1',
      expect.objectContaining({ method: 'DELETE' })
    )
  })
})
