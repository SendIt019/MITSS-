import { useRef, useState } from 'react'

// Shared presentational pieces.

export const VERDICTS = [
  { value: 'unrated', label: 'not reviewed', mark: '·' },
  { value: 'accurate', label: 'accurate', mark: '✓' },
  { value: 'partial', label: 'partly right', mark: '~' },
  { value: 'inaccurate', label: 'inaccurate', mark: '✕' },
]

export const verdictMeta = (value) =>
  VERDICTS.find((v) => v.value === value) || VERDICTS[0]

export function Card({ title, hint, children, right, tight }) {
  return (
    <section className={`card${tight ? ' tight' : ''}`}>
      {(title || right) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          {right}
        </div>
      )}
      {hint && <p className="hint">{hint}</p>}
      {children}
    </section>
  )
}

// Status colour always ships with its mark and its label, never colour alone.
export function Verdict({ value, count }) {
  const meta = verdictMeta(value || 'unrated')
  return (
    <span className={`verdict ${meta.value}`}>
      <span className="mark">{meta.mark}</span>
      {meta.label}
      {count > 1 && <span className="count">×{count}</span>}
    </span>
  )
}

export function VerdictPicker({ value, onChange, disabled }) {
  return (
    <div className="verdict-picker" role="radiogroup" aria-label="Verdict">
      {VERDICTS.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          disabled={disabled}
          className={`verdict-option ${option.value}${value === option.value ? ' on' : ''}`}
          onClick={() => onChange(option.value)}
        >
          <span className="mark">{option.mark}</span>
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function Dropzone({ onFile, busy, label }) {
  const [over, setOver] = useState(false)
  const inputRef = useRef(null)

  const take = (fileList) => {
    const file = fileList?.[0]
    if (file) onFile(file)
  }

  return (
    <div
      className={`dropzone${over ? ' over' : ''}`}
      onClick={() => inputRef.current?.click()}
      onDragOver={(event) => { event.preventDefault(); setOver(true) }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        take(event.dataTransfer.files)
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".txt,.text,.md,.prompt"
        style={{ display: 'none' }}
        onChange={(event) => {
          take(event.target.files)
          event.target.value = ''
        }}
      />
      {busy ? <span>reading…</span> : (
        <>
          <div><strong>{label || 'Drop a .txt prompt here'}</strong> or click to choose</div>
        </>
      )}
    </div>
  )
}

export function CopyButton({ text, label = 'Copy prompt', className = '' }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className={className}
      disabled={!text}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          // Clipboard permission can be refused; the text is on screen anyway.
        }
        setCopied(true)
        setTimeout(() => setCopied(false), 1600)
      }}
    >
      {copied ? 'Copied' : label}
    </button>
  )
}

export function Tabs({ value, onChange, items }) {
  return (
    <div className="tabs" role="tablist">
      {items.map((item) => (
        <button
          key={item.value}
          role="tab"
          aria-selected={value === item.value}
          className={`tab${value === item.value ? ' on' : ''}`}
          onClick={() => onChange(item.value)}
          disabled={item.disabled}
        >
          {item.label}
          {item.badge != null && <span className="badge">{item.badge}</span>}
        </button>
      ))}
    </div>
  )
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>
}

export function Field({ label, children }) {
  return (
    <div className="grow">
      <label className="field">{label}</label>
      {children}
    </div>
  )
}

export function Stat({ label, value, sub }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

export function timeAgo(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const seconds = Math.floor((Date.now() - then) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  return iso.slice(0, 10)
}
