import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { subscribeLive, useLiveRefresh, withScanProgress } from '../../api/events'
import { FakeEventSource, emitLive } from '../../test/fakeEventSource'

afterEach(() => {
  vi.useRealTimers()
})

describe('live events (#89)', () => {
  it('shares one stream and closes it when the last subscriber leaves', () => {
    const before = FakeEventSource.instances.length
    const offA = subscribeLive(['scan_started'], () => {})
    const offB = subscribeLive(['skip_recorded'], () => {})
    const opened = FakeEventSource.instances.slice(before)

    expect(opened).toHaveLength(1)
    expect(opened[0].url).toBe('/api/events')
    offA()
    expect(opened[0].closed).toBe(false)
    offB()
    expect(opened[0].closed).toBe(true)
  })

  it('delivers only the subscribed types, plus resync', () => {
    const seen: string[] = []
    const off = subscribeLive(['skip_recorded'], t => seen.push(t))

    emitLive('scan_started', { guid: 'g' })
    emitLive('skip_recorded', { guid: 'g' })
    emitLive('resync')
    off()

    expect(seen).toEqual(['skip_recorded', 'resync'])
  })

  it('treats a reconnect as a resync', () => {
    const seen: string[] = []
    const off = subscribeLive(['skip_recorded'], t => seen.push(t))
    const es = FakeEventSource.instances[FakeEventSource.instances.length - 1]

    es.onopen?.()   // first open: nothing missed yet
    es.onopen?.()   // reopened after a drop
    off()

    expect(seen).toEqual(['resync'])
  })

  it('debounces bursts, honours relevance, and runs the fallback timer', () => {
    vi.useFakeTimers()
    const refresh = vi.fn()
    const { unmount } = renderHook(() =>
      useLiveRefresh(['segments_changed'], refresh, {
        debounceMs: 100,
        fallbackMs: 1000,
        relevant: (_, d) => d.guid === 'mine',
      }),
    )

    emitLive('segments_changed', { guid: 'other' })
    emitLive('segments_changed', { guid: 'mine' })
    emitLive('segments_changed', { guid: 'mine' })
    vi.advanceTimersByTime(100)
    expect(refresh).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(1000)
    expect(refresh).toHaveBeenCalledTimes(2)
    unmount()
    vi.advanceTimersByTime(5000)
    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('patches progress for one scan only', () => {
    const status = { active_scans: [{ guid: 'a', progress: 0.1 }, { guid: 'b', progress: 0.2 }] }

    expect(withScanProgress(status, { guid: 'b', progress: 0.5 })?.active_scans).toEqual([
      { guid: 'a', progress: 0.1 },
      { guid: 'b', progress: 0.5 },
    ])
  })
})
