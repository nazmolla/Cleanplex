import { translate, type TranslateFn, type TranslationKey } from '../i18n'
import { en } from '../i18n/en'

const defaultT: TranslateFn = (key, vars) => translate('en', key, vars)

function fallbackShortLabel(label: string): string {
  return label
    .replace('_EXPOSED', '')
    .replace('FEMALE_', 'F ')
    .replace('MALE_', 'M ')
    .replace('GENITALIA', 'Gen.')
    .replace('BREAST', 'Breast')
    .replace(/_/g, ' ')
    .trim()
}

function fallbackPrettyLabel(label: string): string {
  const s = label.toLowerCase().replace(/_/g, ' ').replace(/^face (female|male)$/, '$1 face')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Short human-readable label chip text, localized when known. */
export function shortLabel(label: string, t: TranslateFn = defaultT): string {
  const key = `label_short_${label}` as TranslationKey
  if (Object.prototype.hasOwnProperty.call(en, key)) {
    return t(key)
  }
  return fallbackShortLabel(label)
}

/** Full human-readable detector label name, localized when known. */
export function prettyLabel(label: string, t: TranslateFn = defaultT): string {
  const key = `label_pretty_${label}` as TranslationKey
  if (Object.prototype.hasOwnProperty.call(en, key)) {
    return t(key)
  }
  return fallbackPrettyLabel(label)
}
