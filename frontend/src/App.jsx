import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import GraphView, { pairKey } from './components/GraphView.jsx'
import Timeline from './components/Timeline.jsx'
import VerdictPanel from './components/VerdictPanel.jsx'
import MemoryPanel from './components/MemoryPanel.jsx'
import { getJSON, postJSON, streamInvestigation } from './api.js'

const EXAMPLES = [
  'Which accounts are linked to flagged transaction T-9001?',
  'Do we have accounts that collect many small transfers and then cash out?',
  'Which accounts might be fake identities of the same person?',
]

export default function App() {
  const [analysts, setAnalysts] = useState([])
  const [analyst, setAnalyst] = useState('')
  const [question, setQuestion] = useState(EXAMPLES[0])
  const [health, setHealth] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)
  const [timeline, setTimeline] = useState([])
  const [steps, setSteps] = useState([])
  const [verdict, setVerdict] = useState(null)
  const [scores, setScores] = useState({})
  const [memoryNodes, setMemoryNodes] = useState(new Set())
  const [memory, setMemory] = useState([])
  const [tab, setTab] = useState('verdict')
  const [selectedPath, setSelectedPath] = useState(null)
  const [focusId, setFocusId] = useState(null)
  const [replayN, setReplayN] = useState(null)
  const [busyFeedback, setBusyFeedback] = useState(null)
  const [feedbackNote, setFeedbackNote] = useState(null)
  const abort = useRef(null)
  const timer = useRef(null)
  const asked = useRef(new Set())

  useEffect(() => {
    getJSON('/api/health').then(setHealth).catch((e) => setError(e.message))
    getJSON('/api/analysts').then((d) => { setAnalysts(d.analysts); setAnalyst(d.analysts[0]) }).catch((e) => setError(e.message))
    return () => { abort.current?.abort(); clearInterval(timer.current) }
  }, [])

  const loadMemory = useCallback((a) => getJSON(`/api/memory/${a}`).then((d) => setMemory(d.findings)).catch(() => {}), [])
  const resetView = () => {
    clearInterval(timer.current)
    setTimeline([]); setSteps([]); setVerdict(null); setScores({}); setMemoryNodes(new Set())
    setSelectedPath(null); setFocusId(null); setReplayN(null); setError(null); setFeedbackNote(null); asked.current = new Set()
  }

  useEffect(() => { if (analyst) { loadMemory(analyst); resetView() } }, [analyst, loadMemory])

  // Colour account nodes by risk: ask the backend for any score we don't have yet.
  const accountIds = useMemo(() => [...new Set(steps.flatMap((s) => s.nodes).filter((n) => n.kind === 'Account').map((n) => n.id))], [steps])
  const refreshScores = useCallback((ids) => {
    if (!analyst || !ids.length) return
    postJSON('/api/scores', { analyst_id: analyst, account_ids: ids.slice(0, 60) })
      .then((d) => setScores((s) => ({ ...s, ...d.scores }))).catch(() => {})
  }, [analyst])
  useEffect(() => {
    const missing = accountIds.filter((id) => !(id in scores) && !asked.current.has(id))
    if (!missing.length) return
    missing.forEach((id) => asked.current.add(id))
    const t = setTimeout(() => refreshScores(missing), 250)
    return () => clearTimeout(t)
  }, [accountIds, scores, refreshScores])

  const onEvent = (e) => {
    if (e.type === 'thought') setTimeline((t) => [...t, { kind: 'thought', text: e.text }])
    else if (e.type === 'tool_call') setTimeline((t) => [...t, { kind: 'tool', id: e.id, name: e.name, args: e.args, action: e.action, pending: true }])
    else if (e.type === 'tool_result') {
      const has = e.subgraph?.nodes?.length > 0
      setTimeline((t) => t.map((x) => (x.id === e.id ? { ...x, pending: false, ok: e.ok, summary: e.summary, hasGraph: has } : x)))
      if (has) setSteps((s) => [...s, { id: e.id, name: e.name, nodes: e.subgraph.nodes, edges: e.subgraph.edges }])
      if (e.score) setScores((s) => ({ ...s, [e.score.account]: e.score.final }))
    } else if (e.type === 'memory') setMemoryNodes((m) => new Set([...m, e.entity, ...e.findings.flatMap((f) => f.about)]))
    else if (e.type === 'verdict') { setVerdict(e.verdict); setTab('verdict'); loadMemory(analyst) }
    else if (e.type === 'error') setError(e.message)
  }

  const run = async () => {
    if (running || !question.trim()) return
    resetView()
    setRunning(true)
    abort.current = new AbortController()
    try { await streamInvestigation(analyst, question.trim(), onEvent, abort.current.signal) }
    catch (e) { if (e.name !== 'AbortError') setError(e.message) }
    finally { setRunning(false) }
  }

  const replay = () => {
    if (!steps.length || running) return
    clearInterval(timer.current)
    setFocusId(null); setSelectedPath(null); setReplayN(0)
    let n = 0
    timer.current = setInterval(() => {
      n += 1; setReplayN(n)
      if (n >= steps.length) { clearInterval(timer.current); setTimeout(() => setReplayN(null), 2200) }
    }, 1100)
  }

  const sendFeedback = async (findingId, v) => {
    setBusyFeedback(findingId)
    try {
      const r = await postJSON('/api/feedback', { analyst_id: analyst, finding_id: findingId, verdict: v })
      setFeedbackNote(r.score_changes)
      setScores((s) => ({ ...s, ...Object.fromEntries(r.score_changes.map((c) => [c.account, c.after])) }))
      await loadMemory(analyst)
    } catch (e) { setError(e.message) } finally { setBusyFeedback(null) }
  }

  // ---- what is highlighted on the graph: replayed hop > clicked step > clicked path > all evidence paths
  const visibleSteps = replayN == null ? steps : steps.slice(0, replayN)
  const { hNodes, hLinks } = useMemo(() => {
    const nodes = new Set(), links = new Set()
    const addPath = (p) => { p.forEach((n) => nodes.add(n)); for (let i = 0; i < p.length - 1; i++) links.add(pairKey(p[i], p[i + 1])) }
    if (replayN != null && replayN > 0 && steps[replayN - 1]) {
      const s = steps[replayN - 1]; s.nodes.forEach((n) => nodes.add(n.id)); s.edges.forEach((e) => links.add(pairKey(e.source, e.target)))
    } else if (focusId) {
      const s = steps.find((x) => x.id === focusId)
      if (s) { s.nodes.forEach((n) => nodes.add(n.id)); s.edges.forEach((e) => links.add(pairKey(e.source, e.target))) }
    } else if (verdict?.evidence_paths) {
      (selectedPath != null ? [verdict.evidence_paths[selectedPath]] : verdict.evidence_paths).filter(Boolean).forEach(addPath)
    }
    return { hNodes: nodes, hLinks: links }
  }, [replayN, focusId, selectedPath, verdict, steps])

  const usedIds = useMemo(() => new Set((verdict?.memory_used || []).flatMap((m) => m.findings.map((f) => f.finding_id))), [verdict])

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-line px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold tracking-tight">FraudTrace</h1>
          <span className="hidden text-sm text-mute sm:inline">An agent that investigates fraud on a graph, remembers what it learns, and shows its work</span>
        </div>
        <span className={`flex items-center gap-2 text-xs ${health?.ok ? 'text-evidence' : 'text-mute'}`}>
          <i className={`h-2 w-2 rounded-full ${health?.ok ? 'bg-evidence' : 'bg-mute'}`} />{health?.ok ? 'Connected to FalkorDB' : 'Connecting'}
        </span>
      </header>

      <main className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[360px_minmax(0,1fr)_400px]">
        {/* ---------- left: ask + live steps ---------- */}
        <section className="flex min-h-0 flex-col border-line lg:border-r">
          <div className="space-y-3 border-b border-line p-4">
            <label className="block text-sm">
              <span className="mb-1 block text-mute">Analyst (each has a private memory)</span>
              <select value={analyst} onChange={(e) => setAnalyst(e.target.value)} disabled={running}
                className="w-full rounded-md border border-line bg-raised px-3 py-2 text-sm capitalize">
                {analysts.map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-mute">Question</span>
              <textarea value={question} onChange={(e) => setQuestion(e.target.value)} rows={3} maxLength={500}
                onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) run() }}
                className="w-full resize-none rounded-md border border-line bg-raised px-3 py-2 text-sm leading-snug" />
            </label>
            <div className="flex flex-wrap gap-1.5">
              {EXAMPLES.map((q) => <button key={q} onClick={() => setQuestion(q)} className="rounded-full border border-line px-2.5 py-1 text-xs text-mute hover:border-steel hover:text-text">{q.length > 34 ? q.slice(0, 33) + '…' : q}</button>)}
            </div>
            <button onClick={run} disabled={running || !analyst}
              className="w-full rounded-md bg-evidence px-3 py-2 text-sm font-semibold text-ink hover:brightness-110 disabled:opacity-50">
              {running ? 'Investigating…' : 'Investigate'}
            </button>
            {health && !health.api_key_set && <p className="rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber">ANTHROPIC_API_KEY is not set on the backend. Add it to .env and restart the backend.</p>}
            {error && <p className="rounded border border-coral/50 bg-coral/10 p-2 text-xs text-coral">{error}</p>}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4"><Timeline items={timeline} running={running} onPick={(id) => setFocusId(focusId === id ? null : id)} activeId={focusId} /></div>
        </section>

        {/* ---------- center: graph ---------- */}
        <section className="relative min-h-[420px] border-line lg:border-r">
          <div className="absolute left-3 top-3 z-10 flex items-center gap-3">
            <button onClick={replay} disabled={!steps.length || running}
              className="rounded-md border border-line bg-panel/90 px-3 py-1.5 text-sm hover:border-evidence disabled:opacity-40">Replay investigation</button>
            {replayN != null && <span className="text-xs text-mute">Hop {Math.min(replayN, steps.length)} of {steps.length}{steps[replayN - 1] ? ` · ${steps[replayN - 1].name.replace(/_/g, ' ')}` : ''}</span>}
          </div>
          <GraphView steps={visibleSteps} scores={scores} memoryNodes={memoryNodes} highlightNodes={hNodes} highlightLinks={hLinks} />
        </section>

        {/* ---------- right: verdict + memory ---------- */}
        <section className="flex min-h-0 flex-col">
          <div role="tablist" className="flex border-b border-line">
            {[['verdict', 'Verdict'], ['memory', `What I remember (${memory.filter((f) => f.status !== 'superseded').length})`]].map(([k, label]) => (
              <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
                className={`flex-1 px-4 py-3 text-sm ${tab === k ? 'border-b-2 border-evidence font-semibold' : 'text-mute hover:text-text'}`}>{label}</button>
            ))}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            {tab === 'verdict'
              ? <VerdictPanel v={verdict} selectedPath={selectedPath} onSelectPath={(i) => { setSelectedPath(i); setFocusId(null) }} />
              : (
                <div className="space-y-3">
                  {feedbackNote && (
                    <div className="rounded border border-evidence/40 bg-evidence/5 p-2.5 text-xs">
                      <div className="mb-1 font-medium text-evidence">Risk scores updated by your feedback</div>
                      {feedbackNote.slice(0, 6).map((c) => <div key={c.account} className="font-mono">{c.account}: {c.before} → {c.after}</div>)}
                    </div>
                  )}
                  <MemoryPanel findings={memory.filter((f) => f.status !== 'superseded')} usedIds={usedIds} onFeedback={sendFeedback} busyId={busyFeedback} />
                </div>
              )}
          </div>
        </section>
      </main>
    </div>
  )
}
