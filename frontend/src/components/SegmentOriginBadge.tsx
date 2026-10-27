import { segmentOrigin } from '../utils/segmentOrigin'

export default function SegmentOriginBadge({ seg }: { seg: { source?: string; detector?: string } }) {
  const label = segmentOrigin(seg)
  return (
    <span
      title={`Found by ${label}`}
      className="text-[10px] px-1.5 py-0.5 rounded border bg-sky-500/10 border-sky-500/20 text-sky-300 whitespace-nowrap"
    >
      {label}
    </span>
  )
}
