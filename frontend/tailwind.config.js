/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        plex: {
          orange: '#e5a00d',
          dark: '#1f2326',
          darker: '#181b1e',
          card: '#282d33',
          border: '#3a4049',
        },
        // Quiet text. Both are measured against plex-card (#282d33), the lightest
        // surface text sits on: muted is 4.7:1 (any text), faint is 3.2:1 (icons,
        // separators and other non-text marks only). Tailwind's gray-500/600 are
        // 2.8:1 and 2.0:1 there, so do not use them for text (#204).
        muted: '#8f97a6',
        faint: '#727a89',
      },
    },
  },
  plugins: [],
}
