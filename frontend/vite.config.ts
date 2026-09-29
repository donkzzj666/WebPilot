import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

function readPort(name: string, fallback: number): number {
  const port = Number(process.env[name] ?? fallback)
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error(`${name} must be an integer between 1024 and 65535`)
  }
  return port
}

export default defineConfig(() => {
  const apiPort = readPort('WEBAGENT_API_PORT', 8000)
  const uiPort = readPort('WEBAGENT_UI_PORT', 5173)
  const proxy = {
    '/api': {
      target: `http://127.0.0.1:${apiPort}`,
      rewrite: (path: string) => path.replace(/^\/api/, ''),
    },
  }
  return {
    plugins: [react()],
    server: { host: '127.0.0.1', port: uiPort, strictPort: true, proxy },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
  }
})
