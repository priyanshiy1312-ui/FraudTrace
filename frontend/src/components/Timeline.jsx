import React, { useEffect, useRef } from 'react'

const TOOL_LABEL = {
  recall_past_findings: 'Recall memory', save_finding: 'Save finding', record_feedback: 'Record feedback',
  flag_accounts: 'Flag accounts', hold_transaction: 'Hold transaction', create_case: 'Open case',
}
const MEMORY_TOOLS = new Set(['recall_past_findings', 'save_finding', 'record_feedback'])
const pretty = (n) => TOOL_LABEL[n] || n.replace(/_/g, ' ')
const argText = (a) => Object.entries(a || {}).map(([k, v]) => `${k}=${Array.isArray(v) ? `[${v.length}]` : String(v).slice(0, 38)}`).join('  ')

export default function Timeline({ items, running, onPick, activeId }) {
  const end = useRef()
  useEffect(() => { end.current && end.current.scrollIntoView({ block: 'end', behavior: 'smooth' }) }, [items.length, running])

  if (!items.length && !running) {
    return <p className="px-1 text-sm leading-relaxed text-mute">Each thought, tool call and result will show up here as the agent works, so you can follow its reasoning.</p>
  }
  return (
    <ol className="space-y-2.5">
      {items.map((it, i) => it.kind === 'thought' ? (
        <li key={i} className="border-l-2 border-line pl-3 text-sm italic leading-snug text-mute">{it.text}</li>
      ) : (
        <li key={i}>
          <button onClick={() => onPick && it.hasGraph && onPick(it.id)}
            className={`w-full rounded-md border px-3 py-2 text-left transition-colors ${activeId === it.id ? 'border-evidence bg-evidence/10' : 'border-line bg-raised hover:border-steel'} ${it.action ? 'ring-1 ring-coral/50' : ''}`}>
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-sm font-medium">
                <span className={`h-2 w-2 rounded-full ${it.pending ? 'pulse bg-amber' : it.ok === false ? 'bg-coral' : it.action ? 'bg-coral' : MEMORY_TOOLS.has(it.name) ? 'bg-memory' : 'bg-evidence'}`} />
                {pretty(it.name)}
              </span>
              <span className="text-xs text-mute">{it.action ? 'action' : MEMORY_TOOLS.has(it.name) ? 'memory' : 'read'}</span>
            </div>
            <div className="mt-1 truncate font-mono text-xs text-mute">{argText(it.args)}</div>
            {it.summary && <div className={`mt-1.5 line-clamp-3 break-words font-mono text-xs ${it.ok === false ? 'text-coral' : 'text-text/80'}`}>{it.summary}</div>}
          </button>
        </li>
      ))}
      {running && <li className="flex items-center gap-2 pl-1 text-sm text-mute"><span className="pulse h-2 w-2 rounded-full bg-amber" />Agent is working</li>}
      <div ref={end} />
    </ol>
  )
}
