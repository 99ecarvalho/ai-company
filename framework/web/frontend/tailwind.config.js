/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{html,js,svelte,ts}'],
  darkMode: 'class',
  theme: {
    screens: {
      xs: '480px',
      sm: '640px',
      md: '768px',
      lg: '1024px',
      xl: '1280px',
      '2xl': '1536px'
    },
    extend: {
      borderColor: {
        DEFAULT: 'var(--border)'
      },
      colors: {
        bg: 'var(--bg)',
        fg: 'var(--fg)',
        muted: 'var(--muted)',
        accent: 'var(--accent)',
        accent2: 'var(--accent2)',
        ok: 'var(--ok)',
        warn: 'var(--warn)',
        panel: 'var(--panel)',
        panel2: 'var(--panel2)',
        border: 'var(--border)',
        'on-accent': 'var(--on-accent)',
        'bubble-recv': 'var(--bubble-recv)',
        'bubble-recv-border': 'var(--bubble-recv-border)',
        'bubble-sent': 'var(--bubble-sent)',
        'bubble-sent-border': 'var(--bubble-sent-border)'
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI"',
          'Roboto',
          'sans-serif'
        ]
      },
      width: {
        sidebar: 'var(--sidebar-w)',
        rail: 'var(--rail-w)'
      },
      height: {
        bottomNav: 'var(--bottom-nav-h)'
      },
      spacing: {
        tap: '44px',
        fab: '56px'
      }
    }
  },
  plugins: []
};
