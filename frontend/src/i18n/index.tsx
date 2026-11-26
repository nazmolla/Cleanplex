import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { en, type PluralForms } from './en'
import { ar } from './ar'

export type Lang = 'en' | 'ar'
export type Dir = 'ltr' | 'rtl'
export type TranslationKey = keyof typeof en
export type TranslationVars = Record<string, string | number>
export type TranslateFn = (key: TranslationKey, vars?: TranslationVars) => string

export { en, ar }

export const LANG_STORAGE_KEY = 'cleanplex.lang'

const DICTIONARIES: Record<Lang, Record<TranslationKey, string | PluralForms>> = { en, ar }
const PLURAL_RULES: Record<Lang, Intl.PluralRules> = {
  en: new Intl.PluralRules('en'),
  ar: new Intl.PluralRules('ar'),
}

export function intlLocale(lang: Lang): string {
  return lang === 'ar' ? 'ar-u-nu-latn' : 'en-US'
}

export function readStoredLang(): Lang {
  try {
    const v = localStorage.getItem(LANG_STORAGE_KEY)
    return v === 'ar' ? 'ar' : 'en'
  } catch {
    return 'en'
  }
}

export function writeStoredLang(lang: Lang): void {
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lang)
  } catch {
    // Storage may be unavailable (private mode, blocked site data).
  }
}

function resolvePlural(lang: Lang, entry: PluralForms, count: number): string {
  const category = PLURAL_RULES[lang].select(count) as keyof PluralForms
  return entry[category] ?? entry.other
}

export function translate(lang: Lang, key: TranslationKey, vars?: TranslationVars): string {
  const dict = DICTIONARIES[lang] ?? en
  const entry = dict[key] ?? en[key]
  const template = typeof entry === 'string'
    ? entry
    : resolvePlural(lang, entry, Number(vars?.count ?? 0))
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (match, name) => (
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match
  ))
}

function toDate(value: Date | number | string): Date | null {
  const d = value instanceof Date ? value : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatNumberFor(lang: Lang, value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(intlLocale(lang), options).format(value)
}

export function formatDateFor(lang: Lang, value: Date | number | string, options?: Intl.DateTimeFormatOptions): string {
  const d = toDate(value)
  if (!d) return ''
  return new Intl.DateTimeFormat(intlLocale(lang), options).format(d)
}

export function formatDateTimeFor(lang: Lang, value: Date | number | string, options?: Intl.DateTimeFormatOptions): string {
  const d = toDate(value)
  if (!d) return ''
  return new Intl.DateTimeFormat(
    intlLocale(lang),
    options ?? { year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' },
  ).format(d)
}

export function formatTimeFor(lang: Lang, value: Date | number | string, options?: Intl.DateTimeFormatOptions): string {
  const d = toDate(value)
  if (!d) return ''
  return new Intl.DateTimeFormat(
    intlLocale(lang),
    options ?? { hour: 'numeric', minute: '2-digit', second: '2-digit' },
  ).format(d)
}

export interface I18nContextValue {
  lang: Lang
  dir: Dir
  setLang: (lang: Lang) => void
  toggleLang: () => void
  t: TranslateFn
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string
  formatDate: (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string
  formatDateTime: (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string
  formatTime: (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string
}

function makeContextValue(lang: Lang, setLang: (l: Lang) => void): I18nContextValue {
  return {
    lang,
    dir: lang === 'ar' ? 'rtl' : 'ltr',
    setLang,
    toggleLang: () => setLang(lang === 'ar' ? 'en' : 'ar'),
    t: (key, vars) => translate(lang, key, vars),
    formatNumber: (value, options) => formatNumberFor(lang, value, options),
    formatDate: (value, options) => formatDateFor(lang, value, options),
    formatDateTime: (value, options) => formatDateTimeFor(lang, value, options),
    formatTime: (value, options) => formatTimeFor(lang, value, options),
  }
}

const DEFAULT_CONTEXT: I18nContextValue = makeContextValue('en', () => {})

const I18nContext = createContext<I18nContextValue>(DEFAULT_CONTEXT)

export function I18nProvider({ children, initialLang }: { children: ReactNode; initialLang?: Lang }) {
  const [lang, setLangState] = useState<Lang>(() => initialLang ?? readStoredLang())
  const dir: Dir = lang === 'ar' ? 'rtl' : 'ltr'

  useEffect(() => {
    document.documentElement.lang = lang
    document.documentElement.dir = dir
  }, [lang, dir])

  const setLang = useCallback((next: Lang) => {
    writeStoredLang(next)
    setLangState(next)
  }, [])

  const value = useMemo(() => makeContextValue(lang, setLang), [lang, setLang])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useT(): I18nContextValue {
  return useContext(I18nContext)
}
