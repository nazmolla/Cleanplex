import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, CheckCircle, X } from 'lucide-react'

type Tone = 'error' | 'success'
interface ToastItem {
  id: number
  tone: Tone
  message: string
}

// Module-level store, so any handler can raise a toast without a context provider
// (and components under test need no wrapper).
let nextId = 1
let items: ToastItem[] = []
const listeners = new Set<(items: ToastItem[]) => void>()
const MAX_VISIBLE = 4
const SUCCESS_MS = 4000 // long enough to read one line; errors stay until dismissed

function emit() {
  listeners.forEach(fn => fn(items))
}

function dismiss(id: number) {
  items = items.filter(t => t.id !== id)
  emit()
}

function push(tone: Tone, message: string) {
  const id = nextId++
  items = [...items, { id, tone, message }].slice(-MAX_VISIBLE)
  emit()
  if (tone === 'success') setTimeout(() => dismiss(id), SUCCESS_MS)
}

/** Non-blocking messages (#205). Use these, never alert(). */
export const toast = {
  error: (message: string) => push('error', message),
  success: (message: string) => push('success', message),
}

/** Test helper: drop every toast between tests. */
export function clearToasts() {
  items = []
  emit()
}

/** Renders the toasts. Mounted once, in Layout. */
export function Toaster() {
  const [list, setList] = useState(items)
  // Subscribe for the life of the app shell.
  useEffect(() => {
    listeners.add(setList)
    return () => {
      listeners.delete(setList)
    }
  }, [])

  return createPortal(
    <div className="fixed z-[60] bottom-20 md:bottom-4 right-4 left-4 sm:left-auto sm:w-96 flex flex-col gap-2 pointer-events-none">
      {/* Two live regions: errors interrupt, confirmations wait their turn. */}
      <div role="alert" className="flex flex-col gap-2">
        {list.filter(t => t.tone === 'error').map(t => <ToastCard key={t.id} item={t} />)}
      </div>
      <div role="status" className="flex flex-col gap-2">
        {list.filter(t => t.tone === 'success').map(t => <ToastCard key={t.id} item={t} />)}
      </div>
    </div>,
    document.body,
  )
}

function ToastCard({ item }: { item: ToastItem }) {
  const error = item.tone === 'error'
  return (
    <div className="pointer-events-auto flex items-start gap-2.5 rounded-lg border border-plex-border bg-plex-dark px-3 py-2.5 text-sm text-gray-200 shadow-xl">
      {error
        ? <AlertCircle size={16} className="text-red-400 flex-shrink-0 mt-0.5" />
        : <CheckCircle size={16} className="text-green-400 flex-shrink-0 mt-0.5" />}
      <p className="flex-1 min-w-0 break-words whitespace-pre-line">{item.message}</p>
      <button
        type="button"
        onClick={() => dismiss(item.id)}
        aria-label="Dismiss"
        className="p-0.5 rounded text-gray-400 hover:text-white transition-colors flex-shrink-0"
      >
        <X size={14} />
      </button>
    </div>
  )
}
