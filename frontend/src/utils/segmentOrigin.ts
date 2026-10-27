/** Human-readable name for what produced a segment (#92). */

const DETECTORS: Record<string, string> = {
  'nudenet-320n': 'NudeNet 320n',
  'nudenet-640m': 'NudeNet 640m',
  'siglip-base-224': 'SigLIP',
}

const SOURCES: Record<string, string> = {
  // Scanner rows written before the detector column existed: model unknown.
  scanner: 'NudeNet',
  siglip: 'SigLIP',
  subtitles: 'Subtitles',
  skp: 'VideoSkip (.skp)',
  edl: 'EDL (.edl)',
  mcf: 'MovieContentFilter (.mcf)',
  paste: 'Pasted list',
}

export function segmentOrigin(seg: { source?: string; detector?: string }): string {
  const detector = seg.detector || ''
  if (detector) {
    const custom = detector.endsWith('-custom')
    const base = custom ? detector.slice(0, -'-custom'.length) : detector
    const name = DETECTORS[base] ?? base
    return custom ? `${name} (custom)` : name
  }
  const source = seg.source || 'scanner'
  return SOURCES[source] ?? source
}
