/** Minimal EventSource stand-in so tests can push live events (#89). */

type Listener = (e: { data: string }) => void

export class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onopen: (() => void) | null = null
  closed = false
  private listeners = new Map<string, Listener[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, fn: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn])
  }

  close() {
    this.closed = true
  }

  emit(type: string, data: Record<string, unknown> = {}) {
    for (const fn of this.listeners.get(type) ?? []) fn({ data: JSON.stringify(data) })
  }
}

/** Deliver an event on every open stream, as the server would. */
export function emitLive(type: string, data: Record<string, unknown> = {}) {
  for (const es of FakeEventSource.instances) if (!es.closed) es.emit(type, data)
}
