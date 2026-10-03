/** Same-origin control-plane requests. Credentials stay inside the local server. */
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!path.startsWith('/api/') || path.includes('\\') || path.includes('#')) {
    throw new Error('Invalid local API path')
  }
  const headers = new Headers(init.headers)
  headers.set('X-WebPilot-Client', '1')
  headers.delete('Authorization')
  return fetch(path, { ...init, headers, cache: 'no-store', credentials: 'omit',
    mode: 'same-origin', redirect: 'error' })
}
