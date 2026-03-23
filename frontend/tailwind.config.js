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
      },
    },
  },
  plugins: [],
}
