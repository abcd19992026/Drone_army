/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: '#0f1216',
          card: '#171c22',
          cardElevated: '#1f262e',
          line: '#262d36',
          input: '#0d1013'
        },
        tactical: {
          ok: '#2fbf71',
          warn: '#e0a51b',
          bad: '#e0533d',
          cyan: '#00e5ff',
          blue: '#2f6fed',
          purple: '#a855f7'
        }
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif']
      },
      keyframes: {
        pulseUrgent: {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.6', transform: 'scale(1.02)' }
        },
        beacon: {
          '0%, 100%': { opacity: '0.3', transform: 'scale(1)' },
          '50%': { opacity: '0.8', transform: 'scale(1.3)' }
        }
      },
      animation: {
        'pulse-urgent': 'pulseUrgent 1.2s infinite ease-in-out',
        'beacon': 'beacon 2s infinite ease-out'
      }
    },
  },
  plugins: [],
}
