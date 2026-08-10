// Thin wrapper over the backend. Every call goes through request() so error
// handling and JSON decoding live in exactly one place.

const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, options)
  const contentType = response.headers.get('content-type') || ''

  if (!response.ok) {
    let detail = `request failed (${response.status})`
    if (contentType.includes('application/json')) {
      const body = await response.json().catch(() => null)
      if (body && body.detail) detail = body.detail
    } else {
      const text = await response.text().catch(() => '')
      if (text) detail = text
    }
    const error = new Error(detail)
    error.status = response.status
    throw error
  }

  if (contentType.includes('application/json')) return response.json()
  return response.text()
}

export const api = {
  health: () => request('/api/health'),

  llm: () => request('/api/llm'),

  upload(file) {
    const form = new FormData()
    form.append('file', file)
    return request('/api/uploads', { method: 'POST', body: form })
  },

  attachPlan(runId, raw) {
    return request(`/api/runs/${runId}/plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raw }),
    })
  },

  ingest(runId, raw, model, note) {
    return request(`/api/runs/${runId}/ingest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raw, model, note }),
    })
  },

  solve(runId) {
    return request(`/api/runs/${runId}/solve`, { method: 'POST' })
  },

  runs: () => request('/api/runs'),

  run: (runId) => request(`/api/runs/${runId}`),

  diff: (a, b) => request(`/api/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),

  csvUrl: (runId) => `${BASE}/api/runs/${runId}/export.csv`,
}
