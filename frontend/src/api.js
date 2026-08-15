// Thin wrapper over the backend. Error handling and decoding live here only.

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

const json = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  health: () => request('/api/health'),
  llm: () => request('/api/llm'),
  verdicts: () => request('/api/verdicts'),
  activity: (limit = 50) => request(`/api/activity?limit=${limit}`),

  prompts: () => request('/api/prompts'),
  prompt: (id, version) =>
    request(`/api/prompts/${id}${version ? `?version=${version}` : ''}`),
  createPrompt: (name, text, note) =>
    request('/api/prompts', json('POST', { name, text, note })),
  addVersion: (id, text, note) =>
    request(`/api/prompts/${id}/versions`, json('POST', { text, note })),
  renamePrompt: (id, name) => request(`/api/prompts/${id}`, json('PATCH', { name })),

  upload(file, promptId = '', note = '') {
    const form = new FormData()
    form.append('file', file)
    form.append('prompt_id', promptId)
    form.append('note', note)
    return request('/api/uploads', { method: 'POST', body: form })
  },

  recordRun: (payload) => request('/api/runs', json('POST', payload)),
  runs: (promptId) =>
    request(`/api/runs${promptId ? `?prompt_id=${promptId}` : ''}`),
  run: (id) => request(`/api/runs/${id}`),
  review: (id, verdict, notes) =>
    request(`/api/runs/${id}`, json('PATCH', { verdict, notes })),
  deleteRun: (id) => request(`/api/runs/${id}`, { method: 'DELETE' }),
  generate: (promptId, version, model, inputId = '') =>
    request('/api/generate', json('POST',
      { prompt_id: promptId, version, model, input_id: inputId })),

  inputs: () => request('/api/inputs'),
  input: (id) => request(`/api/inputs/${id}`),
  createInput: (name, text, note) =>
    request('/api/inputs', json('POST', { name, text, note })),
  updateInput: (id, patch) => request(`/api/inputs/${id}`, json('PATCH', patch)),
  deleteInput: (id) => request(`/api/inputs/${id}`, { method: 'DELETE' }),
  uploadInput(file) {
    const form = new FormData()
    form.append('file', file)
    return request('/api/inputs/upload', { method: 'POST', body: form })
  },

  preview: (promptId, version, inputId = '') =>
    request(`/api/prompts/${promptId}/preview?${new URLSearchParams({
      ...(version ? { version } : {}), ...(inputId ? { input_id: inputId } : {}),
    })}`),

  transcriptUrl: (download = true) =>
    `${BASE}/api/transcript${download ? '?download=true' : ''}`,
  transcript: (limit) =>
    request(`/api/transcript${limit ? `?limit=${limit}` : ''}`),
  transcriptLocation: () => request('/api/transcript/location'),

  matrix: (promptId, inputId) =>
    request(`/api/prompts/${promptId}/matrix${inputId ? `?input_id=${inputId}` : ''}`),
  compare: (a, b) => request(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  compareVersions: (promptId, a, b) =>
    request(`/api/prompts/${promptId}/compare-versions?a=${a}&b=${b}`),
}
