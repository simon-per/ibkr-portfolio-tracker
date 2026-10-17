// vitest/config, not vite: the same defineConfig widened to accept the `test` block
// below, which vite's own types reject.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    // Per-file, via the `@vitest-environment jsdom` docblock. The pure lib tests are
    // the large majority and run an order of magnitude faster in node; paying jsdom's
    // startup for all of them to suit the few component tests is the wrong default.
    environment: 'node',
    globals: false,
    setupFiles: ['./src/testSetup.ts'],
  },
  build: {
    rollupOptions: {
      output: {
        /**
         * Split the big third-party libraries out of the app chunk.
         *
         * This is about **caching, not payload**: the VPS redeploys within 10 minutes of
         * any push, and every deploy re-hashes the one chunk that holds both our code and
         * ~500 kB of Recharts. Users therefore re-downloaded the whole bundle for a
         * one-line change. Vendor code changes only when a dependency does, so pinning it
         * to its own chunk lets the browser keep it across deploys.
         *
         * Recharts stays eagerly loaded on purpose — three components on the *default*
         * Performance tab use it, so it is needed at first paint and deferring it would
         * only move the wait. Splitting the seven non-default tabs (see `Dashboard.tsx`)
         * is what actually shrinks the initial download.
         */
        // Listed by the specifier that actually appears in the module graph. Naming the
        // bare packages (`react`, `react-dom`) emitted a 0-byte `react` chunk and left
        // React in the app bundle: nothing imports bare `react-dom` — it is
        // `react-dom/client` — and JSX compiles to `react/jsx-runtime`.
        manualChunks: {
          react: ['react', 'react/jsx-runtime', 'react-dom/client'],
          charts: ['recharts'],
          query: ['@tanstack/react-query'],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
