import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
  },
  server: {
    host: true,
    allowedHosts: true,
    proxy: {
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      '/webhook': {
        target: 'http://localhost:8000',
      },
      '/incidents': {
        target: 'http://localhost:8000',
      },
      '/health': {
        target: 'http://localhost:8000',
      }
    }
  }
})
