import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the FastAPI backend, so the frontend can use
// same-origin relative URLs and never needs to know the backend's port.
export default defineConfig({
  plugins: [react()],
  server: {
    // Bind IPv4 loopback explicitly. Vite otherwise resolves `localhost` to
    // IPv6 [::1] only, so the http://127.0.0.1:5173 URL that dev.sh and the
    // README advertise — and that the backend uses — was refused in a browser.
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.MITSS_API || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
