import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  base: process.env.VITE_BASE ?? '/',
  plugins: [react()],
  server: {
    strictPort: true,
    proxy: { '/api': { target: process.env.API_PROXY ?? 'http://127.0.0.1:8001', ws: true } },
  },
})
