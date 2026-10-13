import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({ command }) => ({ plugins: [react(), { name: 'production-csp', transformIndexHtml: html => command === 'build' ? html.replace("script-src 'self' 'unsafe-inline'", "script-src 'self'") : html }], base: './', server: { host: '127.0.0.1', port: 5173, strictPort: true }, build: { outDir: 'dist' } }));
