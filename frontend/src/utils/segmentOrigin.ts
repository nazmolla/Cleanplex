import { translate, type TranslateFn } from '../i18n'

/** Human-readable name for what produced a segment (#92). */

const DETECTORS: Record<string, string> = {
  'nudenet-320n': 'NudeNet 320n',
  'nudenet-640m': 'NudeNet 640m',
  'siglip-base-224': 'SigLIP',
}

const FIXED_SOURCES: Record<string, string> = {
  // Scanner rows written before the detector column existed: model unknown.
  scanner: 'NudeNet',
  siglip: 'SigLIP',
  skp: 'VideoSkip (.skp)',
  edl: 'EDL (.edl)',
  mcf: 'MovieContentFilter (.mcf)',
}

const defaultT: TranslateFn = (key, vars) => translate('en', key, vars)

function translateSource(source: string, t: TranslateFn): string {
  if (source === 'subtitles') return t('origin_subtitles')
  if (source === 'paste') return t('origin_paste')
  return FIXED_SOURCES[source] ?? source
}

export function segmentOrigin(
  seg: { source?: string; detector?: string },
  t: TranslateFn = defaultT,
): string {
  const detector = seg.detector || ''
  if (detector) {
    const custom = detector.endsWith('-custom')
    const base = custom ? detector.slice(0, -'-custom'.length) : detector
    const name = DETECTORS[base] ?? base
    return custom ? t('origin_custom', { name }) : name
  }
  const source = seg.source || 'scanner'
  return translateSource(source, t)
}

/** Formats a raw origin identifier (detector or source) for display. */
export function originDisplayName(origin: string, t: TranslateFn = defaultT): string {
  if (origin === 'nudenet') return 'NudeNet'
  if (origin.endsWith('-custom')) {
    const base = origin.slice(0, -'-custom'.length)
    return t('origin_custom', { name: DETECTORS[base] ?? base })
  }
  return DETECTORS[origin] ?? translateSource(origin, t)
}
