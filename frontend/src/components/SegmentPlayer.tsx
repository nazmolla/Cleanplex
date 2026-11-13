import { useEffect, useState } from 'react'
import { api } from '../api/client'

interface Props {
  segmentId: number
  autoPlay?: boolean
  className?: string
}

// The server renders the clip before sending a byte (a few seconds, minutes for a long
// 4K segment), so the player says so and gives up with a message rather than spin forever.
const GIVE_UP_S = 300

/**
 * Plays a segment's preview clip, with a loading state while the server renders it and an
 * error message with a Retry button when it cannot (#189).
 */
export default function SegmentPlayer({ segmentId, autoPlay = false, className = '' }: Props) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [message, setMessage] = useState('')
  const [elapsed, setElapsed] = useState(0)
  const [attempt, setAttempt] = useState(0)

  // A new segment or a retry starts over; attempt keys the <video> and cache-busts a failed URL.
  useEffect(() => {
    setState('loading')
    setElapsed(0)
  }, [segmentId, attempt])

  // Ticks once a second while loading; stops on ready/error, and fails the load after GIVE_UP_S.
  useEffect(() => {
    if (state !== 'loading') return
    const timer = setInterval(() => setElapsed(e => e + 1), 1000)
    return () => clearInterval(timer)
  }, [state, segmentId, attempt])

  useEffect(() => {
    if (state === 'loading' && elapsed >= GIVE_UP_S) {
      setMessage('The preview is taking too long to render. Try again, or open the title in Plex.')
      setState('error')
    }
  }, [state, elapsed])

  const src = `/api/segments/${segmentId}/preview${attempt ? `?retry=${attempt}` : ''}`

  const onError = async () => {
    setState('error')
    setMessage('Could not load the preview.')
    try {
      const problem = await api.mediaProblem(src)
      if (!problem.ok) setMessage(problem.detail)
    } catch {
      setMessage('Could not reach the server.')
    }
  }

  return (
    <div>
      {state === 'error' ? (
        <div role="alert" className="rounded-lg bg-black/60 border border-red-500/30 p-4 text-sm text-red-300 flex items-center justify-between gap-3">
          <span>{message}</span>
          <button
            onClick={() => setAttempt(a => a + 1)}
            className="px-3 py-1.5 text-xs bg-plex-card border border-plex-border rounded-lg text-gray-300 hover:text-white hover:border-gray-500 flex-shrink-0"
          >
            Retry
          </button>
        </div>
      ) : (
        <>
          {state === 'loading' && (
            <p role="status" className="text-xs text-gray-400 mb-2">
              Rendering the preview… {elapsed}s. Long segments can take a minute.
            </p>
          )}
          <video
            key={`${segmentId}-${attempt}`}
            controls
            playsInline
            autoPlay={autoPlay}
            src={src}
            onCanPlay={() => setState('ready')}
            onError={onError}
            className={`w-full rounded-lg bg-black ${className}`}
          />
        </>
      )}
    </div>
  )
}
