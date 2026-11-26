import { segmentOrigin } from '../utils/segmentOrigin'
import { useT } from '../i18n'

export default function SegmentOriginBadge({ seg }: { seg: { source?: string; detector?: string } }) {
  const { t } = useT()
  const label = segmentOrigin(seg, t)
  return (
    <span
      title={t('origin_found_by', { label })}
      className="text-[10px] px-1.5 py-0.5 rounded border bg-sky-500/10 border-sky-500/20 text-sky-300 whitespace-nowrap"
    >
      {label}
    </span>
  )
}
