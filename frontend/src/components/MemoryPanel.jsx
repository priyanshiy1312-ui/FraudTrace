import React from 'react'

const STATUS = {
  active: ['text-steel border-steel/40', 'Active'], confirmed: ['text-evidence border-evidence/40', 'Confirmed'],
  false_positive: ['text-amber border-amber/40', 'False positive'], superseded: ['text-mute border-line', 'Superseded'],
}

export default function MemoryPanel({ findings, usedIds, onFeedback, busyId }) {
  if (!findings.length) {
    return <p className="text-sm leading-relaxed text-mute">Nothing remembered for this analyst yet. Findings the agent saves will appear here and be reused in later investigations.</p>
  }
  return (
    <ul className="space-y-2.5">
      {findings.map((f) => {
        const [cls, label] = STATUS[f.status] || STATUS.active
        const canJudge = f.status === 'active' || f.status === 'confirmed' || f.status === 'false_positive'
        return (
          <li key={f.finding_id} className="rounded-md border border-line bg-raised p-3">
            <div className="flex items-center justify-between gap-2">
              <span className={`rounded border px-1.5 py-0.5 text-xs ${cls}`}>{label}</span>
              {usedIds.has(f.finding_id) && <span className="text-xs text-memory">Used in this run</span>}
            </div>
            <p className="mt-2 text-sm leading-snug">{f.statement}</p>
            <p className="mt-1.5 break-words font-mono text-xs text-mute">{f.about.slice(0, 6).join(' ')}{f.about.length > 6 ? ` +${f.about.length - 6}` : ''}</p>
            {canJudge && (
              <div className="mt-2.5 flex gap-2">
                <button disabled={busyId === f.finding_id || f.status === 'confirmed'} onClick={() => onFeedback(f.finding_id, 'confirmed')}
                  className="rounded border border-line px-2.5 py-1 text-xs hover:border-evidence disabled:opacity-40">Confirm</button>
                <button disabled={busyId === f.finding_id || f.status === 'false_positive'} onClick={() => onFeedback(f.finding_id, 'false_positive')}
                  className="rounded border border-line px-2.5 py-1 text-xs hover:border-amber disabled:opacity-40">Mark false positive</button>
              </div>
            )}
          </li>
        )
      })}
    </ul>
  )
}
