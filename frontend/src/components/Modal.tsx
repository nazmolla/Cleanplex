import { useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

interface Props {
  title: string
  /** Second line under the title, e.g. a segment's time range. */
  subtitle?: ReactNode
  onClose: () => void
  /** False while a request is running: Escape, the backdrop and the close button do nothing. */
  dismissible?: boolean
  size?: 'sm' | 'md' | 'xl'
  children: ReactNode
  /** Right-aligned buttons under the body. */
  footer?: ReactNode
}

const WIDTH = { sm: 'max-w-sm', md: 'max-w-lg', xl: 'max-w-4xl' }

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], [tabindex]:not([tabindex="-1"])'

/**
 * The one dialog (#205): every overlay in the app goes through it, so they all trap
 * focus, close on Escape or a backdrop click, lock the page behind them and hand focus
 * back to the control that opened them.
 */
export default function Modal({ title, subtitle, onClose, dismissible = true, size = 'md', children, footer }: Props) {
  const panelRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  // Read through refs so the effect below runs once per open, not on every parent render.
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const dismissibleRef = useRef(dismissible)
  dismissibleRef.current = dismissible

  // Runs once per open: move focus in, trap Tab, close on Escape, restore focus on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    const panel = panelRef.current
    if (!panel) return
    const focusables = () => Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
    // A control marked data-autofocus wins (Cancel in a destructive confirmation); otherwise the first
    // control that is not the close button.
    ;(panel.querySelector<HTMLElement>('[data-autofocus]')
      ?? focusables().find(el => !el.hasAttribute('data-modal-close'))
      ?? panel).focus()

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        if (dismissibleRef.current) closeRef.current()
        return
      }
      if (e.key !== 'Tab') return
      const items = focusables()
      if (items.length === 0) {
        e.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || !panel.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !panel.contains(active))) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey, true)
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.body.style.overflow = overflow
      opener?.focus?.()
    }
  }, [])

  return createPortal(
    <div
      className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center p-4"
      onMouseDown={e => {
        // Only a press that starts on the backdrop closes: a drag out of an input must not.
        if (e.target === e.currentTarget && dismissible) onClose()
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`w-full ${WIDTH[size]} max-h-[90vh] flex flex-col bg-plex-card border border-plex-border rounded-xl overflow-hidden`}
      >
        <div className="flex items-start gap-3 px-4 py-3 border-b border-plex-border">
          <div className="flex-1 min-w-0">
            <h2 id={titleId} className="text-sm font-semibold text-gray-100 truncate">{title}</h2>
            {subtitle && <p className="text-xs text-muted mt-0.5">{subtitle}</p>}
          </div>
          <button
            type="button"
            data-modal-close
            onClick={onClose}
            disabled={!dismissible}
            aria-label="Close"
            className="p-1 -m-1 rounded-lg text-gray-400 hover:text-white hover:bg-white/5 transition-colors disabled:opacity-40"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-4 overflow-y-auto">{children}</div>
        {footer && <div className="flex justify-end gap-2 px-4 py-3 border-t border-plex-border">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}
