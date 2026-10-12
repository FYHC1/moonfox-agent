import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://localhost:8765', ws: true, changeOrigin: false },
    },
  },
  build: { target: 'es2022' },
});
