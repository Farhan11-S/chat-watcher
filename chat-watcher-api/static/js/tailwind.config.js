/**
 * tailwind.config.js — Shared Tailwind CSS configuration
 * Loaded before the Tailwind CDN script on every page.
 */
tailwind = window.tailwind || {}
window.tailwindConfig = {
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#f0f9ff',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
        },
      },
    },
  },
}
