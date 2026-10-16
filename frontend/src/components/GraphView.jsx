import React, { useEffect, useMemo, useRef } from 'react'
import ForceGraph2D from 'react-force-graph-2d'

export const riskColor = (s) => (s == null ? '#6f8fae' : s >= 60 ? '#f0646b' : s >= 30 ? '#e5b23c' : '#6f8fae')
const KIND_COLOR = { Device: '#7fb6d9', IPAddress: '#9aa7ff', Merchant: '#7ac79a', Transaction: '#5d7089', Case: '#f0a15f', Person: '#8798ad' }
export const idOf = (v) => (typeof v === 'object' ? v.id : v)
export const pairKey = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`)

function shape(ctx, kind, x, y, r) {
  ctx.beginPath()
  if (kind === 'Device') ctx.rect(x - r, y - r, 2 * r, 2 * r)
  else if (kind === 'IPAddress') { ctx.moveTo(x, y - r - 1); ctx.lineTo(x + r + 1, y); ctx.lineTo(x, y + r + 1); ctx.lineTo(x - r - 1, y); ctx.closePath() }
  else if (kind === 'Merchant') { ctx.moveTo(x, y - r - 1); ctx.lineTo(x + r + 1, y + r); ctx.lineTo(x - r - 1, y + r); ctx.closePath() }
  else if (kind === 'Case') { for (let i = 0; i < 6; i++) { const a = (Math.PI / 3) * i; ctx.lineTo(x + (r + 2) * Math.cos(a), y + (r + 2) * Math.sin(a)) } ctx.closePath() }
  else ctx.arc(x, y, r, 0, 2 * Math.PI)
}

export default function GraphView({ steps, scores, memoryNodes, highlightNodes, highlightLinks, onSelect }) {
  const fg = useRef()
  const box = useRef()
  const nodeCache = useRef(new Map())
  const linkCache = useRef(new Map())
  const [size, setSize] = React.useState({ w: 600, h: 500 })

  useEffect(() => {
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }))
    ro.observe(box.current)
    return () => ro.disconnect()
  }, [])

  // Node/link objects are cached by id so the layout keeps positions while the graph grows (or replays).
  const data = useMemo(() => {
    const nodes = new Map(), links = new Map()
    for (const s of steps) {
      for (const n of s.nodes) {
        if (nodes.has(n.id)) continue
        let o = nodeCache.current.get(n.id)
        if (!o) { o = { id: n.id, kind: n.kind }; nodeCache.current.set(n.id, o) }
        nodes.set(n.id, o)
      }
      for (const e of s.edges) {
        if (!nodes.has(e.source) || !nodes.has(e.target)) continue
        const k = `${e.source}|${e.target}|${e.type}`
        if (links.has(k)) continue
        let l = linkCache.current.get(k)
        if (!l) { l = { source: e.source, target: e.target, type: e.type }; linkCache.current.set(k, l) }
        links.set(k, l)
      }
    }
    return { nodes: [...nodes.values()], links: [...links.values()] }
  }, [steps])

  useEffect(() => {
    const t = setTimeout(() => fg.current && fg.current.zoomToFit(500, 70), 900)
    return () => clearTimeout(t)
  }, [data.nodes.length])

  const dim = highlightNodes.size > 0

  const paintNode = (node, ctx, scale) => {
    const isAcc = node.kind === 'Account'
    const r = node.kind === 'Transaction' ? 3 : isAcc ? 6.5 : 5
    const lit = !dim || highlightNodes.has(node.id)
    const score = scores[node.id]
    ctx.globalAlpha = lit ? 1 : 0.22
    if (dim && lit) { ctx.beginPath(); ctx.arc(node.x, node.y, r + 5, 0, 2 * Math.PI); ctx.fillStyle = 'rgba(63,208,182,0.22)'; ctx.fill() }
    shape(ctx, node.kind, node.x, node.y, r)
    ctx.fillStyle = isAcc ? riskColor(score) : KIND_COLOR[node.kind] || '#6f8fae'
    ctx.fill()
    if (memoryNodes.has(node.id)) {
      ctx.beginPath(); ctx.setLineDash([2.5, 2.5]); ctx.arc(node.x, node.y, r + 4, 0, 2 * Math.PI)
      ctx.strokeStyle = '#a08bff'; ctx.lineWidth = 1.4; ctx.stroke(); ctx.setLineDash([])
    }
    if (scale > 1.5 || (dim && lit) || (isAcc && score >= 30)) {
      ctx.font = `${Math.max(3.2, 10 / Math.sqrt(scale))}px "JetBrains Mono", monospace`
      ctx.fillStyle = '#c9d5e3'; ctx.textAlign = 'center'; ctx.textBaseline = 'top'
      ctx.fillText(node.id, node.x, node.y + r + 3)
    }
    ctx.globalAlpha = 1
  }

  const linkLit = (l) => highlightLinks.has(pairKey(idOf(l.source), idOf(l.target)))

  return (
    <div ref={box} className="relative h-full w-full overflow-hidden">
      <ForceGraph2D
        ref={fg}
        width={size.w}
        height={size.h}
        graphData={data}
        backgroundColor="rgba(0,0,0,0)"
        nodeRelSize={6}
        nodeCanvasObject={paintNode}
        nodePointerAreaPaint={(n, color, ctx) => { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(n.x, n.y, 9, 0, 2 * Math.PI); ctx.fill() }}
        nodeLabel={(n) => `${n.id} · ${n.kind}${scores[n.id] != null ? ` · risk ${scores[n.id]}` : ''}${memoryNodes.has(n.id) ? ' · remembered' : ''}`}
        linkColor={(l) => (linkLit(l) ? '#3fd0b6' : dim ? 'rgba(80,100,125,0.18)' : l.type === 'LINKED' || l.type === 'PAID' ? 'rgba(160,139,255,0.45)' : 'rgba(110,135,165,0.5)')}
        linkWidth={(l) => (linkLit(l) ? 2.6 : 1)}
        linkLineDash={(l) => (l.type === 'LINKED' ? [3, 3] : null)}
        linkDirectionalArrowLength={(l) => (['SENT', 'TO', 'PAID'].includes(l.type) ? 4 : 0)}
        linkDirectionalArrowRelPos={1}
        linkDirectionalParticles={(l) => (linkLit(l) ? 3 : 0)}
        linkDirectionalParticleWidth={2.4}
        linkDirectionalParticleColor={() => '#3fd0b6'}
        onNodeClick={(n) => onSelect && onSelect(n)}
        cooldownTicks={140}
        onEngineStop={() => fg.current && fg.current.zoomToFit(450, 70)}
        d3VelocityDecay={0.3}
      />
      {data.nodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center px-10 text-center text-mute">
          <p className="max-w-sm text-sm leading-relaxed">The connections the agent explores will appear here as it works. Ask a question on the left to start.</p>
        </div>
      )}
      <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-line bg-panel/90 px-3 py-2 text-xs text-mute">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <Dot c="#6f8fae" t="Account, low risk" /><Dot c="#e5b23c" t="Risk 30+" /><Dot c="#f0646b" t="Risk 60+" />
          <span className="flex items-center gap-1.5"><i className="inline-block h-2.5 w-2.5 rounded-full border border-dashed border-memory" />From memory</span>
          <span className="flex items-center gap-1.5"><i className="inline-block h-0.5 w-4 bg-evidence" />Evidence path</span>
        </div>
        <div className="mt-1 text-[11px]">Circle account · square device · diamond IP · triangle merchant · small dot transaction</div>
      </div>
    </div>
  )
}

const Dot = ({ c, t }) => (
  <span className="flex items-center gap-1.5"><i className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: c }} />{t}</span>
)
