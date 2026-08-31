import { useEffect, useState } from 'react'
import { api } from '../api'
import { Card, CopyButton, Empty, Stat, verdictMeta } from './Panels'

// The compiled view: every prompt, version and model rolled up by verdict.
// The same digest is served as plain text (download / copy) so it can be
// read without the tool or pasted into a model as context.

function Counts({ totals }) {
  if (!totals || totals.total === 0) return <span className="muted">no runs</span>
  return (
    <span className="counts">
      {['accurate', 'partial', 'inaccurate', 'unrated']
        .filter((key) => totals[key] > 0)
        .map((key) => {
          const meta = verdictMeta(key)
          return (
            <span className={`verdict ${key}`} key={key}>
              <span className="mark">{meta.mark}</span>
              {totals[key]} {meta.label}
            </span>
          )
        })}
    </span>
  )
}

export default function Digest({ active }) {
  const [digest, setDigest] = useState(null)
  const [text, setText] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!active) return
    let cancelled = false
    Promise.all([api.digest(), api.digestText()])
      .then(([body, page]) => {
        if (cancelled) return
        setDigest(body)
        setText(page)
        setError('')
      })
      .catch((exc) => { if (!cancelled) setError(exc.message || String(exc)) })
    return () => { cancelled = true }
  }, [active])

  if (error) return <Card title="Digest"><Empty>{error}</Empty></Card>
  if (!digest) return <Card title="Digest"><Empty>Compiling…</Empty></Card>

  const totals = digest.totals

  return (
    <>
      <Card
        title="Where things stand"
        hint={`Every verdict is yours, compiled. Generated ${digest.generated_at}.`}
        right={
          <div className="row">
            <CopyButton text={text} label="Copy as text" />
            <a href={api.digestUrl(true)} download className="chip link">
              download .txt
            </a>
          </div>
        }
      >
        <div className="tiles">
          <Stat label="Outputs" value={totals.total} />
          <Stat label="Accurate" value={totals.accurate} />
          <Stat label="Partly right" value={totals.partial} />
          <Stat label="Inaccurate" value={totals.inaccurate} />
          <Stat label="To review" value={digest.unreviewed} />
        </div>

        {digest.models.length > 0 && (
          <>
            <h3 className="section">By model, across every prompt</h3>
            <div className="digest-rows">
              {digest.models.map((block) => (
                <div className="digest-row" key={block.model}>
                  <span className="run-main mono">{block.model}</span>
                  <Counts totals={block.totals} />
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      {digest.prompts.length === 0 ? (
        <Card title="By prompt"><Empty>Nothing recorded yet.</Empty></Card>
      ) : (
        digest.prompts.map((prompt) => (
          <Card
            key={prompt.id}
            title={prompt.name}
            right={<Counts totals={prompt.totals} />}
            tight
          >
            <div className="digest-rows">
              {prompt.versions.map((version) => (
                <div className="digest-row" key={version.version}>
                  <span className="run-main">
                    v{version.version}
                    {version.note && <span className="muted"> — {version.note}</span>}
                  </span>
                  <Counts totals={version.totals} />
                </div>
              ))}
              {prompt.models.map((block) => (
                <div className="digest-row" key={block.model}>
                  <span className="run-main mono muted">on {block.model}</span>
                  <Counts totals={block.totals} />
                </div>
              ))}
            </div>
            {prompt.unreviewed > 0 && (
              <p className="hint" style={{ margin: '8px 0 0' }}>
                {prompt.unreviewed} output{prompt.unreviewed === 1 ? '' : 's'} still to review.
              </p>
            )}
          </Card>
        ))
      )}
    </>
  )
}
