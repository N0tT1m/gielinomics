import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The hiscores API sends no CORS headers and the query API is not public-origin, so the dev
// server proxies /api and /openapi rather than the browser calling either directly. In
// production the same paths are served from the same origin as the built assets.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: process.env.GIELINOMICS_API ?? 'http://localhost:8080', changeOrigin: true },
      '/openapi': { target: process.env.GIELINOMICS_API ?? 'http://localhost:8080', changeOrigin: true },
    },
  },
})
