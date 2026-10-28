import { useState, type ReactNode } from 'react'

interface Props {
  /** The segment's hover_url; empty until the overnight job has rendered it (#114). */
  src?: string
  className?: string
  children: ReactNode
}

/**
 * Thumbnail box that plays the segment's hover preview over the still while the
 * pointer is on it. The preview is a short silent MP4 that loops, so it behaves
 * like a GIF at a fraction of the size. The <video> exists only while hovered:
 * nothing is downloaded for a list nobody points at, and leaving stops playback.
 */
export default function HoverPreview({ src, className = '', children }: Props) {
  const [hovered, setHovered] = useState(false)
  return (
    <div
      className={`relative ${className}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {children}
      {hovered && src && (
        <video
          src={src}
          autoPlay
          muted
          loop
          playsInline
          aria-label="Segment preview"
          className="absolute inset-0 w-full h-full object-cover pointer-events-none"
        />
      )}
    </div>
  )
}
