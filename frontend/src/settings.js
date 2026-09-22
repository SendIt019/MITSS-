// Per-model generation settings, as the Models form edits them and as a run
// shows them back. The backend validates; this only converts between form
// boxes (strings) and the JSON the API takes.
//
// Blank box = harness default (temperature 0, timeout from MITSS_LLM_TIMEOUT).

export const SETTING_FIELDS = [
  { key: 'temperature', label: 'Temperature', hint: '0 = greedy (default)' },
  { key: 'top_p', label: 'Top-p', hint: '0–1' },
  { key: 'top_k', label: 'Top-k', hint: 'integer' },
  { key: 'min_p', label: 'Min-p', hint: '0–1' },
  { key: 'presence_penalty', label: 'Presence penalty', hint: 'e.g. 1.5' },
  { key: 'max_tokens', label: 'Max tokens', hint: 'blank = server cap' },
  { key: 'seed', label: 'Seed', hint: 'integer' },
  { key: 'timeout', label: 'Timeout (s)', hint: 'blank = MITSS_LLM_TIMEOUT' },
]

export const BLANK_SETTINGS = Object.fromEntries(
  SETTING_FIELDS.map((f) => [f.key, '']).concat([['thinking', '']]),
)

// API settings object -> form values.
export function settingsToForm(settings) {
  const form = { ...BLANK_SETTINGS }
  const s = settings || {}
  for (const { key } of SETTING_FIELDS) {
    if (s[key] !== undefined && s[key] !== null) form[key] = String(s[key])
  }
  const thinking = s.chat_template_kwargs?.enable_thinking
  if (thinking === true) form.thinking = 'on'
  if (thinking === false) form.thinking = 'off'
  return form
}

// Form values -> API settings object. Blank boxes are omitted. A box that
// is not a number is sent as typed so the backend can say which field and why.
export function formToSettings(form) {
  const out = {}
  for (const { key } of SETTING_FIELDS) {
    const raw = (form[key] ?? '').trim()
    if (raw === '') continue
    const number = Number(raw)
    out[key] = Number.isNaN(number) ? raw : number
  }
  if (form.thinking === 'on') out.chat_template_kwargs = { enable_thinking: true }
  if (form.thinking === 'off') out.chat_template_kwargs = { enable_thinking: false }
  return out
}

// One readable line, matching the transcript's `settings:` line.
export function describeSettings(settings) {
  if (!settings) return ''
  const parts = []
  for (const [key, value] of Object.entries(settings)) {
    if (key === 'chat_template_kwargs' && value && typeof value === 'object') {
      for (const [k, v] of Object.entries(value)) parts.push(`${k}=${JSON.stringify(v)}`)
    } else if (key === 'timeout') {
      parts.push(`timeout=${value}s`)
    } else {
      parts.push(`${key}=${value}`)
    }
  }
  return parts.join('  ')
}

// What the model server reported about a run: token counts, why it stopped,
// how fast it went. Never estimated — a missing number stays missing.
export function describeUsage(usage) {
  if (!usage) return ''
  const parts = []
  const { prompt_tokens: inTok, completion_tokens: outTok } = usage
  if (inTok !== undefined || outTok !== undefined) {
    parts.push(`${inTok ?? '?'} in / ${outTok ?? '?'} out tokens`)
  }
  if (usage.tokens_per_second !== undefined) parts.push(`${usage.tokens_per_second} tok/s`)
  if (usage.finish_reason) parts.push(`stopped: ${usage.finish_reason}`)
  if (usage.model_reported) parts.push(`server said: ${usage.model_reported}`)
  return parts.join('  ·  ')
}
