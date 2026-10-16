import React from 'react'
import { riskColor } from './GraphView.jsx'

const VERDICT = {
  fraud_ring: 'Fraud ring', mule_network: 'Mule network', synthetic_identity: 'Synthetic identities',
  suspicious: 'Suspicious', likely_benign: 'Likely benign', inconclusive: 'Inconclusive',
}

export default function VerdictPanel({ v, selectedPath, onSelectPath }) {
  if (!v) {
    return <p className="text-sm leading-relaxed text-mute">The verdict, risk score and evidence appear here when the investigation finishes.</p>
  }
  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-line bg-raised p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-xs text-mute">Verdict</div>
            <div className="mt-0.5 text-xl font-semibold leading-tight">{VERDICT[v.verdict] || v.verdict}</div>
            <div className="mt-1 text-xs text-mute">Confidence {Math.round((v.confidence || 0) * 100)}%</div>
          </div>
          <div className="text-right">
            <div className="text-xs text-mute">Risk score</div>
            <div className="text-4xl font-semibold leading-none" style={{ color: riskColor(v.risk_score) }}>{v.risk_score}</div>
            <div className="mt-1 text-xs text-mute">{v.risk_score_source === 'score_account' ? 'from graph + memory' : 'model estimate'}</div>
          </div>
        </div>
        <p className="mt-3 text-sm leading-relaxed">{v.summary}</p>
      </section>

      {v.key_findings?.length > 0 && (
        <Block title="Key findings"><ul className="list-disc space-y-1.5 pl-4 text-sm leading-snug">{v.key_findings.map((k, i) => <li key={i}>{k}</li>)}</ul></Block>
      )}

      {v.evidence_paths?.length > 0 && (
        <Block title="Evidence paths" hint="Click one to highlight it on the graph">
          <div className="space-y-1.5">
            {v.evidence_paths.map((p, i) => (
              <button key={i} onClick={() => onSelectPath(selectedPath === i ? null : i)}
                className={`block w-full break-words rounded border px-2.5 py-1.5 text-left font-mono text-xs ${selectedPath === i ? 'border-evidence bg-evidence/10' : 'border-line hover:border-steel'}`}>
                {p.join(' → ')}
              </button>
            ))}
          </div>
        </Block>
      )}

      {v.actions_taken?.length > 0 ? (
        <Block title="Actions taken">
          <ul className="space-y-2">
            {v.actions_taken.map((a, i) => (
              <li key={i} className="rounded border border-coral/40 bg-coral/5 p-2.5 text-sm">
                <div className="font-medium">{a.tool.replace(/_/g, ' ')}</div>
                <div className="mt-1 break-words font-mono text-xs text-mute">{JSON.stringify(a.result).slice(0, 220)}</div>
              </li>
            ))}
          </ul>
        </Block>
      ) : <Block title="Actions taken"><p className="text-sm text-mute">No actions were taken.</p></Block>}

      <Block title="Remembered and used" hint="Findings from earlier sessions that shaped this answer">
        {v.memory_used?.length ? (
          <ul className="space-y-1.5 text-sm">
            {v.memory_used.flatMap((m) => m.findings).map((f) => <li key={f.finding_id} className="border-l-2 border-memory pl-2.5 leading-snug">{f.statement}</li>)}
          </ul>
        ) : <p className="text-sm text-mute">Nothing relevant was remembered, so this started fresh.</p>}
      </Block>

      <Block title={`Cypher used (${v.queries_used?.length || 0})`}>
        <details className="rounded border border-line">
          <summary className="px-2.5 py-1.5 text-xs text-mute hover:text-text">Show queries</summary>
          <div className="space-y-2 border-t border-line p-2.5">
            {(v.queries_used || []).map((q, i) => <pre key={i} className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-text/80">{q}</pre>)}
          </div>
        </details>
      </Block>

      {v.limitations?.length > 0 && (
        <Block title="Limitations"><ul className="list-disc space-y-1 pl-4 text-sm leading-snug text-mute">{v.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul></Block>
      )}
    </div>
  )
}

export const Block = ({ title, hint, children }) => (
  <section>
    <h3 className="text-sm font-semibold">{title}</h3>
    {hint && <p className="mb-2 text-xs text-mute">{hint}</p>}
    <div className={hint ? '' : 'mt-2'}>{children}</div>
  </section>
)
