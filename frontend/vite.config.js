import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // Dev proxy to bypass CORS when calling fire.ca.gov directly from the browser.
    // Production swaps this for the real API Gateway endpoint (#30).
    proxy: {
      '/api/calfire': {
        target: 'https://incidents.fire.ca.gov',
        changeOrigin: true,
        rewrite: () => '/umbraco/api/IncidentApi/GeoJsonList?inactive=false',
      },
    },
  },
})
