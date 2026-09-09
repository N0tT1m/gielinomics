import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The hiscores API sends no CORS headers and the query API is not public-origin, so the dev
// server proxies /api and /openapi rather than the browser calling either directly. In
// production the same paths are served from the same origin as the built assets.
//
// /api/ai is a different process -- the Python service in src/Gielinomics.Ai, which owns the
// index and the model -- so it is listed FIRST. Vite matches these in declaration order, and
// with /api first every AI request would go to the .NET API and 404.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api/ai': { target: process.env.GIELINOMICS_AI ?? 'http://localhost:8100', changeOrigin: true },
      '/api': { target: process.env.GIELINOMICS_API ?? 'http://localhost:8080', changeOrigin: true },
      '/openapi': { target: process.env.GIELINOMICS_API ?? 'http://localhost:8080', changeOrigin: true },
    },
  },
})
