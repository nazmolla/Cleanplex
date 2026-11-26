import { useT } from '../i18n'

/**
 * Shows when in the film a segment's thumbnail was taken (#133). Without it, a thumbnail
 * that shows the start of a scene reads as "nothing here" and the segment gets marked a
 * false positive; the time says which instant the frame is and that the segment runs on.
 */
export function formatFrameTime(ms: number): string {
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export default function ThumbnailTime({ ms }: { ms?: number | null }) {
  const { t } = useT()
  if (ms == null) return null
  return (
    <div
      title={t('thumb_time_title')}
      className="absolute top-0.5 end-0.5 bg-black/70 text-[10px] text-gray-300 px-1 py-0.5 rounded"
    >
      {t('thumb_time_frame', { time: formatFrameTime(ms) })}
    </div>
  )
}
