"""Derived projection edges, computed INSIDE the graph from the raw data.

Native FalkorDB algorithms (PageRank, WCC) need edges between nodes of the same
label. Raw data goes Account -> Transaction -> Account, so we derive two layers:

  (Account)-[:PAID   {count, total}]->(Account)   money flow, one edge per account pair
  (Account)-[:LINKED {weight, why}]-(Account)     identity similarity, stored in both directions
        weight = 2/shared device + 1/shared IP + 1/shared address + 2 if >=3 repeated transfers
        an edge is kept only when weight >= LINK_THRESHOLD

These are heuristics that point the agent at places worth looking; the agent still has to verify
with raw evidence (devices, IPs, transactions) before it flags anything.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations

LINK_THRESHOLD = 3
W_DEVICE, W_IP, W_ADDRESS, W_REPEAT = 2, 1, 1, 2


def _batches(rows, size=1000):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def build_projections(graph) -> dict:
    graph.query("MATCH ()-[r:PAID]->() DELETE r")
    graph.query("MATCH ()-[r:LINKED]->() DELETE r")

    # --- PAID: aggregate raw transfers per (sender, receiver) ---------------------
    paid = graph.query(
        "MATCH (a:Account)-[:SENT]->(t:Transaction)-[:TO]->(b:Account) "
        "WITH a, b, count(t) AS n, sum(t.amount) AS s RETURN a.id, b.id, n, s").result_set
    rows = [{"a": a, "b": b, "n": n, "s": round(s, 2)} for a, b, n, s in paid]
    for batch in _batches(rows):
        graph.query("UNWIND $rows AS r MATCH (a:Account {id: r.a}), (b:Account {id: r.b}) "
                    "CREATE (a)-[:PAID {count: r.n, total: r.s}]->(b)", params={"rows": batch})

    # --- LINKED: weigh every kind of similarity per account pair -------------------
    weight: dict[tuple[str, str], int] = defaultdict(int)
    why: dict[tuple[str, str], set] = defaultdict(set)

    def bump(a: str, b: str, w: int, reason: str) -> None:
        key = (a, b) if a < b else (b, a)
        weight[key] += w
        why[key].add(reason)

    for rel, w, reason in (("USED_DEVICE", W_DEVICE, "shared_device"), ("LOGGED_IN_FROM", W_IP, "shared_ip")):
        res = graph.query(f"MATCH (a:Account)-[:{rel}]->(x)<-[:{rel}]-(b:Account) WHERE a.id < b.id "
                          "RETURN a.id, b.id, count(x)").result_set
        for a, b, c in res:
            bump(a, b, w * c, reason)
    for a, b, n in graph.query(
            "MATCH (a:Account)-[:SENT]->(t:Transaction)-[:TO]->(b:Account) "
            "WITH a, b, count(t) AS n WHERE n >= 3 RETURN a.id, b.id, n").result_set:
        bump(a, b, W_REPEAT, "repeated_transfers")
    groups = graph.query("MATCH (p:Person)-[:OWNS]->(a:Account) RETURN p.address, collect(a.id)").result_set
    for _addr, accs in groups:
        if 1 < len(accs) <= 30:                       # ignore absurdly large groups
            for a, b in combinations(sorted(accs), 2):
                bump(a, b, W_ADDRESS, "shared_address")

    links = []
    for (a, b), w in weight.items():
        if w >= LINK_THRESHOLD:
            reason = "+".join(sorted(why[(a, b)]))
            links.append({"a": a, "b": b, "w": w, "why": reason})
            links.append({"a": b, "b": a, "w": w, "why": reason})
    for batch in _batches(links):
        graph.query("UNWIND $rows AS r MATCH (a:Account {id: r.a}), (b:Account {id: r.b}) "
                    "CREATE (a)-[:LINKED {weight: r.w, why: r.why}]->(b)", params={"rows": batch})
    return {"paid_edges": len(rows), "linked_edges": len(links)}
