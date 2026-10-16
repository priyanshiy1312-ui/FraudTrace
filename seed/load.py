"""Load the synthetic dataset into FalkorDB.

Usage:
    python -m seed.load            # load fraud_data once; does nothing if it is already seeded
    python -m seed.load --force    # wipe and reload fraud_data (resets flags/cases; memory graphs are kept)
    python -m seed.load --no-reset # never load, only (re)create schema/indexes
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from seed.generate import Dataset, generate
from shared.projection import build_projections
from shared.schema import (DATA_GRAPH, DEFAULT_ANALYSTS, connect,
                           ensure_data_schema, ensure_memory_schema)

BATCH = 1000
OUT_DIR = Path(__file__).parent / "out"


def _batches(rows: list[dict], size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _run(graph, query: str, rows: list[dict]) -> None:
    """Run one parameterised UNWIND query per batch (fast, and no string-built Cypher)."""
    for batch in _batches(rows):
        graph.query(query, params={"rows": batch})


def wait_for_db(host: str, port: int, password: str | None, tries: int = 30):
    for attempt in range(tries):
        try:
            db = connect(host, port, password)
            db.list_graphs()
            return db
        except Exception as exc:
            print(f"waiting for FalkorDB ({attempt + 1}/{tries}): {exc}")
            time.sleep(2)
    raise SystemExit("FalkorDB not reachable")


def load(db, ds: Dataset, reset: bool = True) -> None:
    graph = db.select_graph(DATA_GRAPH)
    if reset and DATA_GRAPH in db.list_graphs():
        graph.delete()
        graph = db.select_graph(DATA_GRAPH)
    ensure_data_schema(db)   # indexes first so the MATCH-based edge loads are fast

    _run(graph, "UNWIND $rows AS r CREATE (:Person {id: r.id, name: r.name, address: r.address})", ds.persons)
    _run(graph, "UNWIND $rows AS r CREATE (:Account {id: r.id, type: r.type, opened: r.opened, status: r.status})", ds.accounts)
    _run(graph, "UNWIND $rows AS r CREATE (:Device {id: r.id, kind: r.kind, os: r.os})", ds.devices)
    _run(graph, "UNWIND $rows AS r CREATE (:IPAddress {id: r.id, ip: r.ip, country: r.country})", ds.ips)
    _run(graph, "UNWIND $rows AS r CREATE (:Merchant {id: r.id, name: r.name, category: r.category})", ds.merchants)
    _run(graph, "UNWIND $rows AS r CREATE (:Transaction {id: r.id, amount: r.amount, ts: r.ts, status: r.status})", ds.transactions)

    _run(graph, "UNWIND $rows AS r MATCH (p:Person {id: r.src}), (a:Account {id: r.dst}) CREATE (p)-[:OWNS]->(a)", ds.owns)
    _run(graph, "UNWIND $rows AS r MATCH (a:Account {id: r.src}), (d:Device {id: r.dst}) CREATE (a)-[:USED_DEVICE]->(d)", ds.used_device)
    _run(graph, "UNWIND $rows AS r MATCH (a:Account {id: r.src}), (i:IPAddress {id: r.dst}) CREATE (a)-[:LOGGED_IN_FROM]->(i)", ds.logged_in_from)
    _run(graph, "UNWIND $rows AS r MATCH (a:Account {id: r.src}), (t:Transaction {id: r.id}) CREATE (a)-[:SENT]->(t)", ds.transactions)
    to_acc = [t for t in ds.transactions if t["dst_label"] == "Account"]
    to_mer = [t for t in ds.transactions if t["dst_label"] == "Merchant"]
    _run(graph, "UNWIND $rows AS r MATCH (t:Transaction {id: r.id}), (a:Account {id: r.dst}) CREATE (t)-[:TO]->(a)", to_acc)
    _run(graph, "UNWIND $rows AS r MATCH (t:Transaction {id: r.id}), (m:Merchant {id: r.dst}) CREATE (t)-[:TO]->(m)", to_mer)

    print("Building derived PAID / LINKED projections inside FalkorDB...")
    print("  ", build_projections(graph))


def print_counts(db) -> None:
    graph = db.select_graph(DATA_GRAPH)
    for label in ["Person", "Account", "Device", "IPAddress", "Merchant", "Transaction"]:
        n = graph.query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]
        print(f"  {label:<12}{n}")
    for rel in ["OWNS", "USED_DEVICE", "LOGGED_IN_FROM", "SENT", "TO", "PAID", "LINKED"]:
        n = graph.query(f"MATCH ()-[r:{rel}]->() RETURN count(r)").result_set[0][0]
        print(f"  [:{rel}]".ljust(18) + str(n))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--force", action="store_true", help="wipe and reload even if already seeded")
    args = ap.parse_args()

    host = os.getenv("FALKORDB_HOST", "localhost")
    port = int(os.getenv("FALKORDB_PORT", "6379"))
    db = wait_for_db(host, port, os.getenv("FALKORDB_PASSWORD"))

    print("Generating synthetic data...")
    ds = generate()
    seeded = DATA_GRAPH in db.list_graphs() and \
        db.select_graph(DATA_GRAPH).query("MATCH (a:Account) RETURN count(a)").result_set[0][0] >= 1000
    if seeded and not args.force:
        print(f"Graph '{DATA_GRAPH}' is already seeded - skipping load (use --force to reload).")
        ensure_data_schema(db)
    elif not args.no_reset:
        print(f"Loading into graph '{DATA_GRAPH}' (this takes a minute)...")
        load(db, ds, reset=True)
    else:
        ensure_data_schema(db)

    for analyst in DEFAULT_ANALYSTS:
        print(f"Memory graph ready: {ensure_memory_schema(db, analyst)}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "ground_truth.json").write_text(json.dumps(ds.ground_truth, indent=2))
    print("Counts:")
    print_counts(db)
    print(f"Answer key written to {OUT_DIR / 'ground_truth.json'} (not stored in the graph).")


if __name__ == "__main__":
    main()
