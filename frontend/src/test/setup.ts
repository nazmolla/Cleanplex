import '@testing-library/jest-dom'

import { FakeEventSource } from './fakeEventSource'
// jsdom has no EventSource; the live-update layer (src/api/events.ts) uses this one.
;(globalThis as any).EventSource = FakeEventSource
