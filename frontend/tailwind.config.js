/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Hayaa brand (#241): warm browns and the gold of the Hayaa icon, so the
        // app no longer borrows Plex's dark grey and orange.
        hayaa: {
          gold: '#c9973a',
          dark: '#201810',
          darker: '#18120c',
          card: '#2a2118',
          border: '#44372a',
        },
        // Quiet text. Both are measured against hayaa-card (#2a2118), the lightest
        // surface text sits on: muted is 5.7:1 (any text), faint is 3.8:1 (icons,
        // separators and other non-text marks only). Tailwind's gray-500/600 are
        // 2.8:1 and 2.0:1 there, so do not use them for text (#204).
        muted: '#a59a8c',
        faint: '#867b6e',
      },
    },
  },
  plugins: [],
}
