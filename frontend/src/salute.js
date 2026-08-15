// M-SALUTE: the standard structure for an input (the material a prompt is
// applied to). Inputs are stored as plain text, so the form assembles the
// seven fields into a canonical block and parses that block back out again —
// which lets a saved input reopen straight into its fields, no backend schema.
//
//   M  Mission objective (GFC intent)  — what the mission file must achieve
//   S  Size
//   A  Activity
//   L  Location
//   U  Unit
//   T  Time
//   E  Equipment

export const SALUTE_FIELDS = [
  { key: 'm', letter: 'M', label: 'Mission objective (GFC intent)', header: 'MISSION OBJECTIVE (GFC INTENT)', rows: 3 },
  { key: 's', letter: 'S', label: 'Size', header: 'SIZE', rows: 2 },
  { key: 'a', letter: 'A', label: 'Activity', header: 'ACTIVITY', rows: 2 },
  { key: 'l', letter: 'L', label: 'Location', header: 'LOCATION', rows: 2 },
  { key: 'u', letter: 'U', label: 'Unit', header: 'UNIT', rows: 2 },
  { key: 't', letter: 'T', label: 'Time', header: 'TIME', rows: 2 },
  { key: 'e', letter: 'E', label: 'Equipment', header: 'EQUIPMENT', rows: 2 },
]

const HEADER_TO_KEY = Object.fromEntries(SALUTE_FIELDS.map((f) => [f.header, f.key]))

export const emptyFields = () =>
  Object.fromEntries(SALUTE_FIELDS.map((f) => [f.key, '']))

// Assemble the fields into the canonical block that gets stored and rendered
// into {input}. Every field is written even when blank, so a report always
// carries all seven headers in order.
export function assembleSalute(fields) {
  return SALUTE_FIELDS
    .map((f) => `[${f.header}]\n${(fields[f.key] || '').trim()}`)
    .join('\n\n')
    .trim()
}

// Parse a stored block back into fields. Returns null when the text was not
// produced by this form (a legacy free-text input or an upload), so the editor
// can fall back to a raw textarea instead of silently dropping content.
export function parseSalute(text) {
  if (!text) return null
  const buckets = {}
  let current = null
  let matched = false
  for (const line of text.split('\n')) {
    const head = line.match(/^\[(.+?)\]\s*$/)
    const key = head && HEADER_TO_KEY[head[1]]
    if (key) {
      current = key
      buckets[key] = []
      matched = true
    } else if (current) {
      buckets[current].push(line)
    }
  }
  if (!matched) return null
  const fields = emptyFields()
  for (const f of SALUTE_FIELDS) {
    fields[f.key] = (buckets[f.key] || []).join('\n').trim()
  }
  return fields
}

export const hasAnyField = (fields) =>
  SALUTE_FIELDS.some((f) => (fields[f.key] || '').trim())
