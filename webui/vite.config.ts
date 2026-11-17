import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Development calls the Python service directly (CORS is open); the production
// build is served by the same service from the same origin, so no proxy is used.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
  },
})
