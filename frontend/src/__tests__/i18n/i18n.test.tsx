import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import {
  en,
  ar,
  I18nProvider,
  useT,
  translate,
  readStoredLang,
  LANG_STORAGE_KEY,
  type TranslationKey,
} from '../../i18n'
import Layout from '../../components/Layout'

function extractPlaceholders(template: string): string[] {
  return Array.from(template.matchAll(/\{(\w+)\}/g), m => m[1]).sort()
}

describe('i18n dictionary parity', () => {
  it('has identical keys in en and ar', () => {
    const enKeys = Object.keys(en).sort()
    const arKeys = Object.keys(ar).sort()
    expect(arKeys).toEqual(enKeys)
  })

  it('has non-empty Arabic translations with matching placeholders for every key', () => {
    for (const key of Object.keys(en) as TranslationKey[]) {
      const enVal = en[key]
      const arVal = ar[key]

      if (typeof enVal === 'string') {
        expect(typeof arVal, `${key} should be a string in ar`).toBe('string')
        const arStr = arVal as string
        expect(arStr.trim().length, `${key} should be non-empty in ar`).toBeGreaterThan(0)
        expect(extractPlaceholders(arStr), `${key} placeholders mismatch`).toEqual(
          extractPlaceholders(enVal),
        )
      } else {
        expect(typeof arVal, `${key} should be a plural object in ar`).toBe('object')
        const arPlural = arVal as Record<string, string | undefined>
        expect(arPlural.one?.trim().length, `${key}.one should be non-empty in ar`).toBeGreaterThan(0)
        expect(arPlural.other?.trim().length, `${key}.other should be non-empty in ar`).toBeGreaterThan(0)
        const enVars = extractPlaceholders(enVal.other)
        expect(extractPlaceholders(arPlural.other ?? ''), `${key}.other placeholders mismatch`).toEqual(enVars)
      }
    }
  })
})

function HookProbe() {
  const { lang, dir, t, setLang, toggleLang, formatNumber, formatDate } = useT()
  return (
    <div>
      <span data-testid="lang">{lang}</span>
      <span data-testid="dir">{dir}</span>
      <span data-testid="title">{t('dash_title')}</span>
      <span data-testid="plural-1">{t('unit_segment', { count: 1 })}</span>
      <span data-testid="plural-2">{t('unit_segment', { count: 2 })}</span>
      <span data-testid="plural-5">{t('unit_segment', { count: 5 })}</span>
      <span data-testid="num">{formatNumber(1234)}</span>
      <span data-testid="date">{formatDate('2026-05-10T12:00:00Z')}</span>
      <button onClick={toggleLang}>toggle</button>
      <button onClick={() => setLang('ar')}>ar</button>
      <button onClick={() => setLang('en')}>en</button>
    </div>
  )
}

describe('useT and I18nProvider', () => {
  let store: Map<string, string>

  beforeEach(() => {
    store = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v) },
      removeItem: (k: string) => { store.delete(k) },
      clear: () => store.clear(),
    })
    document.documentElement.lang = 'en'
    document.documentElement.dir = 'ltr'
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('defaults to English LTR without a provider', () => {
    render(<HookProbe />)
    expect(screen.getByTestId('lang')).toHaveTextContent('en')
    expect(screen.getByTestId('dir')).toHaveTextContent('ltr')
    expect(screen.getByTestId('title')).toHaveTextContent('Dashboard')
    expect(screen.getByTestId('plural-1')).toHaveTextContent('1 segment')
    expect(screen.getByTestId('plural-5')).toHaveTextContent('5 segments')
  })

  it('switches language, direction, document attributes, and localStorage', () => {
    render(
      <I18nProvider>
        <HookProbe />
      </I18nProvider>,
    )
    expect(document.documentElement.lang).toBe('en')
    expect(document.documentElement.dir).toBe('ltr')

    fireEvent.click(screen.getByText('toggle'))
    expect(screen.getByTestId('lang')).toHaveTextContent('ar')
    expect(screen.getByTestId('dir')).toHaveTextContent('rtl')
    expect(screen.getByTestId('title')).toHaveTextContent(ar.dash_title)
    expect(screen.getByTestId('plural-1')).toHaveTextContent('مقطع واحد')
    expect(screen.getByTestId('plural-2')).toHaveTextContent('مقطعان')
    expect(screen.getByTestId('plural-5')).toHaveTextContent('5 مقاطع')
    expect(screen.getByTestId('num')).toHaveTextContent('1,234')
    expect(store.get(LANG_STORAGE_KEY)).toBe('ar')
    expect(document.documentElement.lang).toBe('ar')
    expect(document.documentElement.dir).toBe('rtl')

    fireEvent.click(screen.getByText('en'))
    expect(screen.getByTestId('lang')).toHaveTextContent('en')
    expect(document.documentElement.dir).toBe('ltr')
    expect(store.get(LANG_STORAGE_KEY)).toBe('en')
  })

  it('handles unavailable localStorage safely', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
    })
    expect(readStoredLang()).toBe('en')
    expect(translate('ar', 'nav_settings')).toBe(ar.nav_settings)
  })

  it('keeps the language choice out of the main navigation', () => {
    render(
      <MemoryRouter>
        <I18nProvider>
          <Layout><div>child</div></Layout>
        </I18nProvider>
      </MemoryRouter>,
    )
    expect(screen.getAllByText('Dashboard').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /العربية/ })).toBeNull()
  })
})
