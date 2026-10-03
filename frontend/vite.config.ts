import { defineConfig } from 'vite'
import type { Plugin, ViteDevServer, PreviewServer } from 'vite'
import react from '@vitejs/plugin-react'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import type { IncomingMessage } from 'node:http'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function readPort(name: string, fallback: number): number {
  const port = Number(process.env[name] ?? fallback)
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error(`${name} must be an integer between 1024 and 65535`)
  }
  return port
}

function values(request: IncomingMessage, name: string): string[] {
  const result: string[] = []
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    if (request.rawHeaders[index].toLowerCase() === name) result.push(request.rawHeaders[index + 1])
  }
  return result
}

// The token is transferred only through an anonymous subprocess pipe and stays
// in this server closure. It is never a VITE_* variable, browser module or URL.
function localToken(): string {
  try {
    const token = execFileSync(resolve(root, '.venv/bin/python'), ['-c',
      'from webagent.config import Settings; from webagent.security.token import load_or_create_token; import sys; sys.stdout.write(load_or_create_token(Settings.from_env().data_dir))'],
    { cwd: root, env: { ...process.env, PYTHONPATH: resolve(root, 'backend') },
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 10000 })
    if (!/^[A-Za-z0-9_-]{64}$/.test(token)) throw new Error('invalid')
    return token
  } catch {
    throw new Error('Local API credential storage is unavailable or unsafe')
  }
}

function localBoundary(uiPort: number, previewPort: number): Plugin {
  function install(server: ViteDevServer | PreviewServer, preview: boolean) {
    const options = preview ? server.config.preview : server.config.server
    const port = preview ? previewPort : uiPort
    if (options.host !== '127.0.0.1' || options.port !== port || !options.strictPort
        || options.cors !== false || server.config.base !== '/') {
      throw new Error('The local workbench requires its configured loopback binding and origin')
    }
    const token = localToken()
    const host = `127.0.0.1:${port}`
    const origin = `http://${host}`
    server.middlewares.use((request, response, next) => {
      response.setHeader('Cache-Control', 'no-store')
      response.setHeader('Cross-Origin-Resource-Policy', 'same-origin')
      response.setHeader('Cross-Origin-Opener-Policy', 'same-origin')
      response.setHeader('X-Frame-Options', 'DENY')
      response.setHeader('X-Content-Type-Options', 'nosniff')
      response.setHeader('Content-Security-Policy', "frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
      const hosts = values(request, 'host')
      const origins = values(request, 'origin')
      const sites = values(request, 'sec-fetch-site')
      let allowed = hosts.length === 1 && hosts[0] === host && origins.length <= 1
        && (!origins.length || origins[0] === origin)
        && (sites.length === 0 || (sites.length === 1 && ['same-origin', 'none'].includes(sites[0])))
      // No cross-origin browser can forge Sec-Fetch-* or send the custom header
      // without a preflight. OPTIONS receives no CORS permission or credential.
      const path = (request.url ?? '').split('?')[0]
      const api = path === '/api' || path.startsWith('/api/')
      if (api) {
        allowed &&= sites.length === 1 && sites[0] === 'same-origin'
          && values(request, 'sec-fetch-dest').join(',') === 'empty'
          && values(request, 'x-webpilot-client').join(',') === '1'
          && request.method !== 'OPTIONS'
      }
      // Never accept a browser-supplied bearer via the UI; only guarded API
      // requests get our server-side credential. No secret enters page JS.
      delete request.headers.authorization
      if (!allowed) {
        response.statusCode = 403
        response.setHeader('Content-Type', 'application/json')
        response.end('{"message":"Local workbench access denied"}')
        return
      }
      if (api) request.headers.authorization = `Bearer ${token}`
      next()
    })
  }
  return { name: 'webpilot-local-boundary', enforce: 'pre',
    configureServer: (server) => install(server, false),
    configurePreviewServer: (server) => install(server, true) }
}

export default defineConfig(() => {
  const apiPort = readPort('WEBAGENT_API_PORT', 8000)
  const uiPort = readPort('WEBAGENT_UI_PORT', 5173)
  const previewPort = readPort('WEBAGENT_PREVIEW_PORT', 4173)
  const proxy = {
    '/api': {
      target: `http://127.0.0.1:${apiPort}`,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(/^\/api/, ''),
    },
  }
  return {
    plugins: [localBoundary(uiPort, previewPort), react()],
    server: { host: '127.0.0.1', port: uiPort, strictPort: true, cors: false,
      allowedHosts: ['127.0.0.1'], proxy,
      fs: { strict: true, allow: [resolve(root, 'frontend')],
        deny: ['.env', '.env.*', '*.{crt,pem}', '**/.git/**', '**/.security/**', '**/local-api-token'] } },
    preview: { host: '127.0.0.1', port: previewPort, strictPort: true, cors: false,
      allowedHosts: ['127.0.0.1'], proxy },
  }
})
