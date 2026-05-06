import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const backend = process.env.VITE_BACKEND || 'http://localhost:8090';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/sw.js': { target: backend, changeOrigin: true },
      '/manifest.webmanifest': { target: backend, changeOrigin: true },
      '/static': { target: backend, changeOrigin: true },
      '/health': { target: backend, changeOrigin: true }
    }
  },
  build: {
    target: 'es2022',
    sourcemap: false
  }
});
