import { useState } from 'react'
import { ImageOff } from 'lucide-react'

/** An image that turns into a plain placeholder when it fails to load, instead of a broken-image icon. */
export default function SafeImg({ src, alt = '', className = '', fit = 'cover', iconSize = 16 }: {
  src?: string
  alt?: string
  className?: string
  fit?: 'cover' | 'contain'
  iconSize?: number
}) {
  // Keyed on src: a new picture gets a new try.
  const [failedSrc, setFailedSrc] = useState('')
  if (!src || failedSrc === src) {
    return <div className={`${className} flex items-center justify-center bg-plex-border/50 text-faint`} aria-hidden="true"><ImageOff size={iconSize} /></div>
  }
  return <img src={src} alt={alt} loading="lazy" onError={() => setFailedSrc(src)} className={`${className} ${fit === 'contain' ? 'object-contain' : 'object-cover'}`} />
}
