"""FraudTrace tools: plain Python functions, exposed over MCP by server.py.

Every read tool returns the same envelope so the UI can explain itself:
    {"data": ..., "cypher": [queries that were run], "subgraph": {"nodes": [...], "edges": [...]}}

Tenant isolation: memory/action tools take `analyst_id`. The MCP server publishes
that argument, but the backend REMOVES it from the schema shown to the model and
injects the logged-in analyst itself, so the model can never address another
analyst's memory graph.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from falkordb import Edge, Node, Path

from shared.guard import UnsafeQuery, make_readonly
from shared.schema import DATA_GRAPH, connect, ensure_memory_schema, memory_graph_name

QUERY_TIMEOUT_MS = 5000
FLAG_THRESHOLD = 30          # minimum graph-evidence score before an action tool will act
PREFIX_KIND = {"A-": "Account", "P-": "Person", "D-": "Device", "IP-": "IPAddress",
               "M-": "Merchant", "T-": "Transaction", "C-": "Case"}
ID_RE = re.compile(r"^[A-Z]{1,2}-\d{1,8}$")
RAW_RELS = {"OWNS", "USED_DEVICE", "LOGGED_IN_FROM", "SENT", "TO", "PAID", "LINKED"}

_db = None


class ToolError(ValueError):
    """Raised for bad input; the MCP layer turns it into a readable tool error."""


# --------------------------------------------------------------------------- helpers
def db():
    global _db
    if _db is None:
        _db = connect(os.getenv("FALKORDB_HOST", "localhost"), int(os.getenv("FALKORDB_PORT", "6379")),
                      os.getenv("FALKORDB_PASSWORD"))
    return _db


def data_graph():
    return db().select_graph(DATA_GRAPH)


def memory_graph(analyst_id: str):
    ensure = memory_graph_name(analyst_id)       # validates the id (raises ValueError if unsafe)
    return db().select_graph(ensure)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kind_of(node_id: str) -> str:
    if not isinstance(node_id, str) or not ID_RE.match(node_id):
        raise ToolError(f"'{node_id}' is not a valid node id (expected e.g. A-0001, D-0042, T-9001)")
    for prefix, kind in PREFIX_KIND.items():
        if node_id.startswith(prefix):
            return kind
    raise ToolError(f"unknown id prefix in '{node_id}'")


def ro(graph, query: str, params: dict | None = None):
    return graph.ro_query(query, params or {}, timeout=QUERY_TIMEOUT_MS).result_set


def envelope(data: Any, cypher: list[str], nodes: set[str] | None = None, edges: list[tuple] | None = None) -> dict:
    node_list = [{"id": n, "kind": PREFIX_KIND.get(n.split("-")[0] + "-", "Node")} for n in sorted(nodes or [])]
    edge_list = [{"source": s, "target": t, "type": ty} for s, t, ty in (edges or [])]
    return {"data": data, "cypher": cypher, "subgraph": {"nodes": node_list, "edges": edge_list}}


def _jsonable(v: Any) -> Any:
    if isinstance(v, Node):
        return {"labels": list(v.labels), "properties": v.properties}
    if isinstance(v, Edge):
        return {"type": v.relation, "properties": v.properties}
    if isinstance(v, Path):
        return {"nodes": [_jsonable(n) for n in v.nodes()], "edges": [_jsonable(e) for e in v.edges()]}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def _num(x) -> float:
    return float(x) if x is not None else 0.0


def _require_account(g, account_id: str) -> None:
    if kind_of(account_id) != "Account":
        raise ToolError(f"{account_id} is not an account id")
    if not ro(g, "MATCH (a:Account {id:$id}) RETURN a.id", {"id": account_id}):
        raise ToolError(f"account {account_id} not found")


# =========================================================================== READ TOOLS
def get_account_profile(account_id: str) -> dict:
    """Profile of one account: owner, devices, IPs, money in/out, flagged transactions, linked accounts."""
    g = data_graph()
    _require_account(g, account_id)
    q_owner = "MATCH (p:Person)-[:OWNS]->(a:Account {id:$id}) RETURN p.id, p.name, p.address, a.type, a.opened, a.status"
    q_ent = ("MATCH (a:Account {id:$id}) OPTIONAL MATCH (a)-[:USED_DEVICE]->(d:Device) "
             "WITH a, collect(DISTINCT d.id) AS devices OPTIONAL MATCH (a)-[:LOGGED_IN_FROM]->(i:IPAddress) "
             "RETURN devices, collect(DISTINCT i.id)")
    q_out = ("MATCH (a:Account {id:$id})-[:SENT]->(t:Transaction) RETURN count(t), sum(t.amount), "
             "sum(CASE WHEN t.status = 'flagged' THEN 1 ELSE 0 END)")
    q_in = "MATCH (t:Transaction)-[:TO]->(a:Account {id:$id}) RETURN count(t), sum(t.amount)"
    q_link = ("MATCH (a:Account {id:$id})-[l:LINKED]->(b:Account) RETURN b.id, l.weight, l.why "
              "ORDER BY l.weight DESC LIMIT 15")
    p = {"id": account_id}
    owner = ro(g, q_owner, p)[0]
    devices, ips = ro(g, q_ent, p)[0]
    n_out, sum_out, n_flag = ro(g, q_out, p)[0]
    n_in, sum_in = ro(g, q_in, p)[0]
    links = ro(g, q_link, p)
    data = {"account_id": account_id, "owner": {"id": owner[0], "name": owner[1], "address": owner[2]},
            "type": owner[3], "opened": owner[4], "status": owner[5], "devices": devices, "ips": ips,
            "sent": {"count": n_out, "total": round(_num(sum_out), 2), "flagged_tx": int(_num(n_flag))},
            "received": {"count": n_in, "total": round(_num(sum_in), 2)},
            "linked_accounts": [{"account": b, "weight": w, "why": why} for b, w, why in links]}
    nodes = {account_id, owner[0], *devices, *ips, *[b for b, _, _ in links]}
    edges = [(owner[0], account_id, "OWNS")] + [(account_id, d, "USED_DEVICE") for d in devices] \
        + [(account_id, i, "LOGGED_IN_FROM") for i in ips] + [(account_id, b, "LINKED") for b, _, _ in links]
    return envelope(data, [q_owner, q_ent, q_out, q_in, q_link], nodes, edges)


def get_transaction_links(tx_id: str) -> dict:
    """Accounts linked to a (flagged) transaction: sender, receiver, their identity links and money partners."""
    g = data_graph()
    if kind_of(tx_id) != "Transaction":
        raise ToolError(f"{tx_id} is not a transaction id")
    q_tx = ("MATCH (s:Account)-[:SENT]->(t:Transaction {id:$id})-[:TO]->(d) "
            "RETURN s.id, t.amount, t.ts, t.status, labels(d)[0], d.id")
    rows = ro(g, q_tx, {"id": tx_id})
    if not rows:
        raise ToolError(f"transaction {tx_id} not found")
    sender, amount, ts, status, dst_kind, dst = rows[0]
    q_link = ("MATCH (a:Account {id:$id})-[l:LINKED]->(b:Account) RETURN b.id, l.weight, l.why "
              "ORDER BY l.weight DESC LIMIT 20")
    q_pay = ("MATCH (a:Account {id:$id})-[p:PAID]-(b:Account) RETURN b.id, p.count, p.total "
             "ORDER BY p.total DESC LIMIT 10")
    linked = ro(g, q_link, {"id": sender})
    partners = ro(g, q_pay, {"id": sender})
    data = {"transaction": {"id": tx_id, "amount": amount, "ts": ts, "status": status},
            "sender": sender, "receiver": {"kind": dst_kind, "id": dst},
            "accounts_identity_linked_to_sender": [{"account": b, "weight": w, "why": why} for b, w, why in linked],
            "money_partners_of_sender": [{"account": b, "transfers": c, "total": round(t, 2)} for b, c, t in partners]}
    nodes = {tx_id, sender, dst, *[b for b, _, _ in linked], *[b for b, _, _ in partners]}
    edges = [(sender, tx_id, "SENT"), (tx_id, dst, "TO")] + [(sender, b, "LINKED") for b, _, _ in linked] \
        + [(sender, b, "PAID") for b, _, _ in partners]
    return envelope(data, [q_tx, q_link, q_pay], nodes, edges)


def get_neighbors(node_id: str, relationship: str | None = None, depth: int = 1, limit: int = 40) -> dict:
    """Nodes reachable from node_id within `depth` (1-3) hops, optionally via one relationship type."""
    g = data_graph()
    label = kind_of(node_id)
    depth, limit = max(1, min(int(depth), 3)), max(1, min(int(limit), 100))
    rel = ""
    if relationship:
        if relationship not in RAW_RELS:                       # whitelist -> safe to put in the query text
            raise ToolError(f"relationship must be one of {sorted(RAW_RELS)}")
        rel = f":{relationship}"
    q = (f"MATCH (n:{label} {{id:$id}})-[{rel}*1..{depth}]-(m) "
         f"RETURN DISTINCT m.id, labels(m)[0] LIMIT {limit}")
    rows = ro(g, q, {"id": node_id})
    data = {"node": node_id, "depth": depth, "relationship": relationship,
            "neighbors": [{"id": i, "kind": k} for i, k in rows], "truncated": len(rows) >= limit}
    edges = []
    if depth == 1:
        q1 = f"MATCH (n:{label} {{id:$id}})-[r{rel}]-(m) RETURN startNode(r).id, type(r), endNode(r).id LIMIT {limit}"
        edges = [(s, t, ty) for s, ty, t in ro(g, q1, {"id": node_id})]
    return envelope(data, [q], {node_id, *[i for i, _ in rows]}, edges)


def find_shared_entities(account_id: str) -> dict:
    """Devices, IPs and home addresses that this account shares with OTHER accounts."""
    g = data_graph()
    _require_account(g, account_id)
    q_dev = ("MATCH (a:Account {id:$id})-[:USED_DEVICE]->(x:Device)<-[:USED_DEVICE]-(o:Account) "
             "WHERE o.id <> $id RETURN x.id, collect(DISTINCT o.id)")
    q_ip = ("MATCH (a:Account {id:$id})-[:LOGGED_IN_FROM]->(x:IPAddress)<-[:LOGGED_IN_FROM]-(o:Account) "
            "WHERE o.id <> $id RETURN x.id, collect(DISTINCT o.id)")
    q_addr = ("MATCH (p:Person)-[:OWNS]->(a:Account {id:$id}) MATCH (q:Person {address: p.address})-[:OWNS]->(o:Account) "
              "WHERE o.id <> $id RETURN p.address, collect(DISTINCT o.id)")
    p = {"id": account_id}
    dev, ips, addr = ro(g, q_dev, p), ro(g, q_ip, p), ro(g, q_addr, p)
    data = {"account": account_id,
            "shared_devices": [{"device": d, "other_accounts": o} for d, o in dev],
            "shared_ips": [{"ip": i, "other_accounts": o[:25], "total_other_accounts": len(o)} for i, o in ips],
            "shared_address": [{"address": a, "other_accounts": o} for a, o in addr]}
    nodes, edges = {account_id}, []
    for d, others in dev:
        nodes |= {d, *others}
        edges += [(account_id, d, "USED_DEVICE")] + [(o, d, "USED_DEVICE") for o in others]
    for i, others in ips:
        nodes |= {i, *others[:25]}
        edges += [(account_id, i, "LOGGED_IN_FROM")] + [(o, i, "LOGGED_IN_FROM") for o in others[:25]]
    return envelope(data, [q_dev, q_ip, q_addr], nodes, edges)


def shortest_path(from_id: str, to_id: str, max_hops: int = 6) -> dict:
    """Shortest connection between two nodes over the raw data (any direction), via algo.SPpaths."""
    g = data_graph()
    a_kind, b_kind = kind_of(from_id), kind_of(to_id)
    max_hops = max(1, min(int(max_hops), 8))
    q = (f"MATCH (a:{a_kind} {{id:$a}}), (b:{b_kind} {{id:$b}}) "
         "CALL algo.SPpaths({sourceNode:a, targetNode:b, relTypes:['OWNS','USED_DEVICE','LOGGED_IN_FROM','SENT','TO'], "
         f"relDirection:'both', maxLen:{max_hops}, pathCount:1}}) YIELD path, pathCost "
         "RETURN [n IN nodes(path) | n.id], [r IN relationships(path) | type(r)], pathCost")
    rows = ro(g, q, {"a": from_id, "b": to_id})
    if not rows:
        return envelope({"connected": False, "max_hops": max_hops}, [q], {from_id, to_id})
    ids, types, cost = rows[0]
    return envelope({"connected": True, "hops": int(cost), "path": ids, "relationships": types}, [q], set(ids),
                    [(ids[i], ids[i + 1], types[i]) for i in range(len(types))])


def detect_communities(min_size: int = 3, limit: int = 8) -> dict:
    """Clusters of accounts joined by strong identity links (weakly connected components over :LINKED)."""
    g = data_graph()
    q = ("CALL algo.WCC({nodeLabels:['Account'], relationshipTypes:['LINKED']}) YIELD node, componentId "
         "WITH componentId, collect(node.id) AS members WHERE size(members) >= $m "
         "RETURN componentId, members, size(members) ORDER BY size(members) DESC LIMIT $lim")
    q_links = ("MATCH (a:Account)-[l:LINKED]->(b:Account) WHERE a.id IN $ids AND b.id IN $ids AND a.id < b.id "
               "RETURN a.id, b.id, l.why LIMIT 150")
    comps, nodes, edges = [], set(), []
    for cid, members, size in ro(g, q, {"m": int(min_size), "lim": int(limit)}):
        links = ro(g, q_links, {"ids": members})          # the REAL :LINKED edges inside this community
        reasons: dict[str, int] = {}
        for _a, _b, why in links:
            reasons[why] = reasons.get(why, 0) + 1
        comps.append({"community_id": cid, "size": size, "accounts": sorted(members), "linked_pairs": len(links),
                      "link_reasons": reasons})
        nodes |= set(members)
        edges += [(a, b, "LINKED") for a, b, _ in links]
    return envelope({"communities": comps}, [q, q_links], nodes, edges)


def rank_suspicious_nodes(limit: int = 10, basis: str = "identity") -> dict:
    """Most central accounts. basis='identity' = PageRank over :LINKED, 'money' = betweenness over :PAID."""
    g = data_graph()
    limit = max(1, min(int(limit), 50))
    if basis == "identity":
        q = ("CALL algo.pageRank('Account', 'LINKED') YIELD node, score "
             "WITH node, score MATCH (node)-[l:LINKED]->() WITH node, score, count(l) AS links "
             "RETURN node.id, score, links ORDER BY score DESC LIMIT $lim")
    elif basis == "money":
        q = ("CALL algo.betweenness({nodeLabels:['Account'], relationshipTypes:['PAID']}) YIELD node, score "
             "RETURN node.id, score, 0 ORDER BY score DESC LIMIT $lim")
    else:
        raise ToolError("basis must be 'identity' or 'money'")
    rows = ro(g, q, {"lim": limit})
    data = {"basis": basis, "ranking": [{"account": a, "score": round(s, 6), "identity_links": l} for a, s, l in rows]}
    return envelope(data, [q], {a for a, _, _ in rows})


def detect_cycles(account_id: str, max_len: int = 12, min_repeats: int = 2) -> dict:
    """Money loops: paths that return to account_id along :PAID edges used at least min_repeats times."""
    g = data_graph()
    _require_account(g, account_id)
    max_len, min_repeats = max(2, min(int(max_len), 14)), max(1, int(min_repeats))
    q = (f"MATCH p=(a:Account {{id:$id}})-[:PAID*2..{max_len}]->(a) "
         "WHERE all(r IN relationships(p) WHERE r.count >= $minr) "
         "RETURN [n IN nodes(p) | n.id], reduce(s = 0.0, r IN relationships(p) | s + r.total) LIMIT 5")
    rows = ro(g, q, {"id": account_id, "minr": min_repeats})
    cycles = [{"path": ids, "length": len(ids) - 1, "total_amount": round(t, 2)} for ids, t in rows]
    nodes, edges = set(), []
    for c in cycles:
        nodes |= set(c["path"])
        edges += [(c["path"][i], c["path"][i + 1], "PAID") for i in range(len(c["path"]) - 1)]
    return envelope({"account": account_id, "cycles": cycles, "min_repeats": min_repeats}, [q], nodes, edges)


def find_mule_candidates(limit: int = 10) -> dict:
    """Accounts that gather many small inbound transfers and then push out most of it in one large transfer."""
    g = data_graph()
    q = ("MATCH (:Account)-[:SENT]->(t:Transaction)-[:TO]->(m:Account) WHERE t.amount <= 250 "
         "WITH m, count(t) AS n_in, sum(t.amount) AS total_in WHERE n_in >= 15 "
         "MATCH (m)-[:SENT]->(o:Transaction)-[:TO]->(c:Account) WHERE o.amount >= total_in * 0.8 "
         "RETURN m.id, n_in, total_in, o.id, o.amount, c.id ORDER BY o.amount DESC LIMIT $lim")
    rows = ro(g, q, {"lim": max(1, min(int(limit), 30))})
    cands = [{"account": m, "small_inbound_count": n, "small_inbound_total": round(tot, 2), "cashout_tx": o,
              "cashout_amount": round(amt, 2), "cashout_to": c} for m, n, tot, o, amt, c in rows]
    nodes, edges = set(), []
    for c in cands:
        nodes |= {c["account"], c["cashout_tx"], c["cashout_to"]}
        edges += [(c["account"], c["cashout_tx"], "SENT"), (c["cashout_tx"], c["cashout_to"], "TO")]
    return envelope({"candidates": cands}, [q], nodes, edges)


def run_readonly_cypher(query: str) -> dict:
    """Run your own read-only Cypher on fraud_data (guarded: no writes/CALL, bounded paths, LIMIT added)."""
    try:
        safe = make_readonly(query)
    except UnsafeQuery as exc:
        raise ToolError(f"query rejected: {exc}")
    res = data_graph().ro_query(safe, {}, timeout=QUERY_TIMEOUT_MS)
    rows = [_jsonable(r) for r in res.result_set]
    return envelope({"columns": [h[1] if isinstance(h, (list, tuple)) else h for h in res.header],
                     "rows": rows, "row_count": len(rows)}, [safe])


# =========================================================================== SCORING
def _signals(g, account_id: str) -> tuple[int, list[dict]]:
    """Graph-evidence score (0-100) for one account, with the factors that produced it."""
    factors: list[dict] = []
    p = {"id": account_id}
    n_links = ro(g, "MATCH (a:Account {id:$id})-[l:LINKED]->(:Account) RETURN count(l)", p)[0][0]
    if n_links:
        factors.append({"factor": "identity_links", "detail": f"{n_links} strongly linked accounts (shared devices/IPs/address)",
                        "points": min(40, 6 * n_links)})
    cyc = ro(g, "MATCH p=(a:Account {id:$id})-[:PAID*2..12]->(a) WHERE all(r IN relationships(p) WHERE r.count >= 3) "
                "RETURN length(p) LIMIT 1", p)
    if cyc:
        factors.append({"factor": "money_cycle", "detail": f"money returns to the account over a {cyc[0][0]}-hop loop of repeated transfers",
                        "points": 30})
    mule = ro(g, "MATCH (:Account)-[:SENT]->(t:Transaction)-[:TO]->(m:Account {id:$id}) WHERE t.amount <= 250 "
                 "WITH m, count(t) AS n_in, sum(t.amount) AS tot WHERE n_in >= 15 "
                 "MATCH (m)-[:SENT]->(o:Transaction) WHERE o.amount >= tot * 0.8 RETURN n_in", p)
    if mule:
        factors.append({"factor": "mule_pattern", "detail": f"{mule[0][0]} small inbound transfers then one large outbound", "points": 35})
    fed = ro(g, "MATCH (m:Account)-[:SENT]->(t:Transaction)-[:TO]->(c:Account {id:$id}) WHERE t.amount > 1000 "
                "MATCH (:Account)-[:SENT]->(s:Transaction)-[:TO]->(m) WHERE s.amount <= 250 "
                "WITH c, m, count(s) AS n_in WHERE n_in >= 15 RETURN count(DISTINCT m)", p)
    if fed and fed[0][0] >= 2:
        factors.append({"factor": "mule_cashout_endpoint", "detail": f"receives large transfers from {fed[0][0]} mule-like accounts", "points": 30})
    n_flag = ro(g, "MATCH (:Account {id:$id})-[:SENT]->(t:Transaction {status:'flagged'}) RETURN count(t)", p)[0][0]
    if n_flag:
        factors.append({"factor": "flagged_transactions", "detail": f"{n_flag} transaction(s) already flagged", "points": min(20, 10 * n_flag)})
    return min(100, sum(f["points"] for f in factors)), factors


_BASE_CACHE: dict[str, tuple[int, list[dict]]] = {}


def base_signals(account_id: str) -> tuple[int, list[dict]]:
    """`_signals` with an in-process cache: the graph evidence only changes when the data is re-seeded."""
    if account_id not in _BASE_CACHE:
        _BASE_CACHE[account_id] = _signals(data_graph(), account_id)
    return _BASE_CACHE[account_id]


def _memory_adjustment(analyst_id: str, account_id: str) -> tuple[int, list[dict]]:
    """How this analyst's own past feedback moves the score (the memory 'changes future risk scoring' rule)."""
    rows = ro(memory_graph(analyst_id),
              "MATCH (f:Finding)-[:ABOUT]->(:Entity {id:$id}) WHERE f.status IN ['false_positive','confirmed'] "
              "RETURN f.id, f.status", {"id": account_id})
    adj, notes = 0, []
    for fid, status in rows:
        delta = -35 if status == "false_positive" else 10
        adj += delta
        notes.append({"finding": fid, "verdict": status, "points": delta})
    return max(-50, min(20, adj)), notes


def score_account(analyst_id: str, account_id: str) -> dict:
    """Risk score 0-100 = graph evidence + this analyst's memory feedback. Use this number as risk_score."""
    g = data_graph()
    _require_account(g, account_id)
    base, factors = base_signals(account_id)
    try:
        adj, notes = _memory_adjustment(analyst_id, account_id)
    except Exception:           # memory graph not created yet -> no feedback yet
        adj, notes = 0, []
    return envelope({"account": account_id, "base_score": base, "memory_adjustment": adj,
                     "final_score": max(0, min(100, base + adj)), "factors": factors, "memory_feedback": notes},
                    ["(see factor queries in mcp_server/tools.py::_signals)"], {account_id})


# =========================================================================== MEMORY TOOLS
def start_investigation(analyst_id: str, question: str) -> str:
    """Create an Investigation node (called by the backend, not by the model)."""
    ensure_memory_schema(db(), analyst_id)
    inv_id = f"INV-{uuid.uuid4().hex[:8]}"
    memory_graph(analyst_id).query("CREATE (:Investigation {id:$id, question:$q, ts:$ts})",
                                   {"id": inv_id, "q": question[:500], "ts": now()})
    return inv_id


def recall_past_findings(analyst_id: str, entity_id: str, include_history: bool = False) -> dict:
    """What this analyst's memory already knows about an entity (account/device/IP/transaction)."""
    kind_of(entity_id)
    g = memory_graph(analyst_id)
    q = ("MATCH (f:Finding)-[:ABOUT]->(:Entity {id:$id}) "
         "OPTIONAL MATCH (i:Investigation)-[:FOUND]->(f) OPTIONAL MATCH (fb:Feedback)-[:ON]->(f) "
         "RETURN f.id, f.statement, f.confidence, f.status, f.ts, f.evidence, i.question, collect(fb.verdict) "
         "ORDER BY f.ts DESC")
    try:
        rows = ro(g, q, {"id": entity_id})
    except Exception:
        rows = []
    findings = []
    for fid, stmt, conf, status, ts, ev, question, verdicts in rows:
        if status == "superseded" and not include_history:
            continue
        ents = [r[0] for r in ro(g, "MATCH (:Finding {id:$f})-[:ABOUT]->(e:Entity) RETURN e.id", {"f": fid})]
        findings.append({"finding_id": fid, "statement": stmt, "confidence": conf, "status": status, "saved_at": ts,
                         "about": ents, "from_question": question, "analyst_feedback": verdicts})
    return envelope({"entity": entity_id, "findings": findings, "note": "empty list = nothing remembered yet"}, [q],
                    {entity_id})


def save_finding(analyst_id: str, investigation_id: str, entity_ids: list[str], statement: str,
                 confidence: float, evidence: list[str]) -> dict:
    """Remember a conclusion. Replaces an older ACTIVE finding about the exact same entities (history is kept)."""
    if not entity_ids or len(entity_ids) > 40:
        raise ToolError("entity_ids must contain 1-40 ids")
    for e in entity_ids:
        kind_of(e)
    if not statement.strip() or len(statement) > 800:
        raise ToolError("statement must be 1-800 characters")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ToolError("confidence must be between 0 and 1")
    if not evidence:
        raise ToolError("evidence is required (cite the tools/queries that support this finding)")
    g = memory_graph(analyst_id)
    ensure_memory_schema(db(), analyst_id)
    ids = sorted(set(entity_ids))
    fid, ts = f"F-{uuid.uuid4().hex[:8]}", now()
    # which active findings talk about exactly this entity set?
    superseded = []
    for (old,) in ro(g, "MATCH (f:Finding {status:'active'})-[:ABOUT]->(e:Entity) WHERE e.id IN $ids RETURN DISTINCT f.id", {"ids": ids}):
        old_ids = sorted(r[0] for r in ro(g, "MATCH (:Finding {id:$f})-[:ABOUT]->(e:Entity) RETURN e.id", {"f": old}))
        if old_ids == ids:
            superseded.append(old)
    g.query("MATCH (i:Investigation {id:$inv}) CREATE (i)-[:FOUND]->(:Finding {id:$fid, statement:$s, confidence:$c, "
            "status:'active', ts:$ts, evidence:$ev})",
            {"inv": investigation_id, "fid": fid, "s": statement.strip(), "c": float(confidence), "ts": ts,
             "ev": json.dumps(evidence)[:3000]})
    g.query("UNWIND $ents AS e MATCH (f:Finding {id:$fid}) MERGE (n:Entity {id:e.id}) SET n.kind = e.kind "
            "CREATE (f)-[:ABOUT]->(n)", {"fid": fid, "ents": [{"id": e, "kind": kind_of(e)} for e in ids]})
    for old in superseded:
        g.query("MATCH (n:Finding {id:$new}), (o:Finding {id:$old}) SET o.status = 'superseded' CREATE (n)-[:SUPERSEDES]->(o)",
                {"new": fid, "old": old})
    # link this investigation to earlier ones that touched the same entities
    g.query("MATCH (i:Investigation {id:$inv}), (o:Investigation)-[:FOUND]->(:Finding)-[:ABOUT]->(e:Entity) "
            "WHERE e.id IN $ids AND o.id <> $inv WITH DISTINCT i, o MERGE (i)-[:FOLLOWS_UP]->(o)",
            {"inv": investigation_id, "ids": ids})
    return {"data": {"finding_id": fid, "saved": True, "superseded": superseded}, "cypher": [], "subgraph": {"nodes": [], "edges": []}}


def record_feedback(analyst_id: str, finding_id: str, verdict: str) -> dict:
    """Analyst verdict on a finding: 'confirmed' or 'false_positive'. Changes future scoring for its entities."""
    if verdict not in ("confirmed", "false_positive"):
        raise ToolError("verdict must be 'confirmed' or 'false_positive'")
    g = memory_graph(analyst_id)
    if not ro(g, "MATCH (f:Finding {id:$id}) RETURN f.id", {"id": finding_id}):
        raise ToolError(f"finding {finding_id} not found in this analyst's memory")
    g.query("MATCH (f:Finding {id:$id}) SET f.status = $v CREATE (:Feedback {id:$fb, verdict:$v, ts:$ts})-[:ON]->(f)",
            {"id": finding_id, "v": verdict, "fb": f"FB-{uuid.uuid4().hex[:8]}", "ts": now()})
    return {"data": {"finding_id": finding_id, "verdict": verdict}, "cypher": [], "subgraph": {"nodes": [], "edges": []}}


def score_many(analyst_id: str, account_ids: list[str]) -> dict[str, int]:
    """Final scores for several accounts (used to colour the graph). Non-accounts are skipped."""
    out: dict[str, int] = {}
    for a in list(dict.fromkeys(account_ids))[:60]:
        if a.startswith("A-") and ID_RE.match(a):
            try:
                out[a] = score_account(analyst_id, a)["data"]["final_score"]
            except ToolError:
                pass
    return out


def list_memory(analyst_id: str) -> dict:
    """Everything one analyst remembers (used by the UI 'What I remembered' panel; not an MCP tool)."""
    g = memory_graph(analyst_id)
    try:
        rows = ro(g, "MATCH (f:Finding) OPTIONAL MATCH (f)-[:ABOUT]->(e:Entity) OPTIONAL MATCH (i:Investigation)-[:FOUND]->(f) "
                     "RETURN f.id, f.statement, f.confidence, f.status, f.ts, collect(e.id), i.question ORDER BY f.ts DESC")
    except Exception:
        rows = []
    return {"analyst": analyst_id, "findings": [
        {"finding_id": a, "statement": b, "confidence": c, "status": d, "saved_at": e, "about": f, "from_question": q}
        for a, b, c, d, e, f, q in rows]}


# =========================================================================== ACTION TOOLS
def _check_evidence(evidence: list[str]) -> None:
    if not evidence or not all(isinstance(e, str) and e.strip() for e in evidence):
        raise ToolError("evidence is required: list the graph facts/tool results that justify this action")


def flag_accounts(analyst_id: str, account_ids: list[str], reason: str, confidence: float, evidence: list[str]) -> dict:
    """ACTION: mark accounts as flagged. Refused for any account whose graph-evidence score is below 30."""
    memory_graph_name(analyst_id)
    _check_evidence(evidence)
    if not account_ids or len(account_ids) > 25:
        raise ToolError("account_ids must contain 1-25 ids")
    g = data_graph()
    flagged, refused = [], []
    for acc in dict.fromkeys(account_ids):
        _require_account(g, acc)
        score, _ = base_signals(acc)
        if score < FLAG_THRESHOLD:
            refused.append({"account": acc, "graph_evidence_score": score,
                            "why": f"below the {FLAG_THRESHOLD}-point evidence threshold"})
            continue
        g.query("MATCH (a:Account {id:$id}) SET a.status = 'flagged', a.flag_reason = $r, a.flag_confidence = $c, "
                "a.flagged_by = $by, a.flagged_at = $ts",
                {"id": acc, "r": reason[:400], "c": float(confidence), "by": analyst_id, "ts": now()})
        flagged.append({"account": acc, "graph_evidence_score": score})
    return {"data": {"flagged": flagged, "refused": refused}, "cypher": [], "subgraph": {"nodes": [{"id": f["account"], "kind": "Account"} for f in flagged], "edges": []}}


def hold_transaction(analyst_id: str, tx_id: str, reason: str, evidence: list[str]) -> dict:
    """ACTION: put a transaction on hold. Only allowed when its sender or receiver has graph-evidence score >= 30."""
    memory_graph_name(analyst_id)
    _check_evidence(evidence)
    g = data_graph()
    if kind_of(tx_id) != "Transaction":
        raise ToolError(f"{tx_id} is not a transaction id")
    rows = ro(g, "MATCH (s:Account)-[:SENT]->(t:Transaction {id:$id})-[:TO]->(d) RETURN s.id, labels(d)[0], d.id", {"id": tx_id})
    if not rows:
        raise ToolError(f"transaction {tx_id} not found")
    sender, dkind, dst = rows[0]
    best = max([base_signals(sender)[0]] + ([base_signals(dst)[0]] if dkind == "Account" else []))
    if best < FLAG_THRESHOLD:
        return {"data": {"held": False, "graph_evidence_score": best,
                         "why": f"neither side reaches the {FLAG_THRESHOLD}-point evidence threshold"},
                "cypher": [], "subgraph": {"nodes": [], "edges": []}}
    g.query("MATCH (t:Transaction {id:$id}) SET t.status = 'held', t.hold_reason = $r, t.held_by = $by, t.held_at = $ts",
            {"id": tx_id, "r": reason[:400], "by": analyst_id, "ts": now()})
    return {"data": {"held": True, "tx_id": tx_id, "graph_evidence_score": best}, "cypher": [],
            "subgraph": {"nodes": [{"id": tx_id, "kind": "Transaction"}], "edges": []}}


def create_case(analyst_id: str, account_ids: list[str], summary: str, evidence: list[str]) -> dict:
    """ACTION: open a Case node that INCLUDES the given accounts (at least one must meet the evidence threshold)."""
    memory_graph_name(analyst_id)
    _check_evidence(evidence)
    if not account_ids or len(account_ids) > 40:
        raise ToolError("account_ids must contain 1-40 ids")
    g = data_graph()
    accs = list(dict.fromkeys(account_ids))
    for a in accs:
        _require_account(g, a)
    if max(base_signals(a)[0] for a in accs) < FLAG_THRESHOLD:
        raise ToolError(f"refused: no account reaches the {FLAG_THRESHOLD}-point graph-evidence threshold")
    case_id = f"C-{uuid.uuid4().int % 10**6:06d}"
    g.query("CREATE (:Case {id:$id, summary:$s, opened_by:$by, opened_at:$ts, evidence:$ev, status:'open'})",
            {"id": case_id, "s": summary[:600], "by": analyst_id, "ts": now(), "ev": json.dumps(evidence)[:3000]})
    g.query("MATCH (c:Case {id:$id}) UNWIND $accs AS a MATCH (x:Account {id:a}) CREATE (c)-[:INCLUDES]->(x)",
            {"id": case_id, "accs": accs})
    return {"data": {"case_id": case_id, "accounts": accs}, "cypher": [],
            "subgraph": {"nodes": [{"id": case_id, "kind": "Case"}] + [{"id": a, "kind": "Account"} for a in accs],
                         "edges": [{"source": case_id, "target": a, "type": "INCLUDES"} for a in accs]}}


# name -> function; server.py registers these and the tests iterate over them
READ_TOOLS = [get_account_profile, get_transaction_links, get_neighbors, find_shared_entities, shortest_path,
              detect_communities, rank_suspicious_nodes, detect_cycles, find_mule_candidates, run_readonly_cypher]
MEMORY_TOOLS = [score_account, recall_past_findings, save_finding, record_feedback]
ACTION_TOOLS = [flag_accounts, hold_transaction, create_case]
