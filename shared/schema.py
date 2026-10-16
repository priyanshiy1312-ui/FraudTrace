"""Graph names, schema and indexes shared by the seed script, MCP server and backend.

Two kinds of graphs live in one FalkorDB instance:
  * `fraud_data`          - the shared, mostly read-only business graph.
  * `memory_<analyst_id>` - one isolated graph per analyst (tenant) holding
                            what the agent has learned for that analyst.
"""
from __future__ import annotations

import os
import re

from falkordb import FalkorDB

DATA_GRAPH = "fraud_data"
DEFAULT_ANALYSTS = ["alice", "bob"]

# ---- fraud_data schema (documentation + index list) -------------------------
# Nodes
#   Person(id, name, address)            Account(id, type, opened, status)
#   Device(id, kind, os)                 IPAddress(id, ip, country)
#   Merchant(id, name, category)         Transaction(id, amount, ts, status)
# Relationships
#   (Person)-[:OWNS]->(Account)
#   (Account)-[:USED_DEVICE]->(Device)
#   (Account)-[:LOGGED_IN_FROM]->(IPAddress)
#   (Account)-[:SENT]->(Transaction)-[:TO]->(Account | Merchant)
#   Derived (built by shared/projection.py): (Account)-[:PAID {count,total}]->(Account), (Account)-[:LINKED {weight,why}]->(Account)
#   Actions add: (Case)-[:INCLUDES]->(Account); Account.status = active|flagged; Transaction.status adds `held`
# Note: `address` is a property on Person (shared addresses are found by
# matching on that property). `status` on Transaction: completed | flagged | held.
DATA_INDEXES = [
    ("Person", "id"), ("Person", "address"), ("Account", "id"),
    ("Device", "id"), ("IPAddress", "id"), ("Merchant", "id"),
    ("Transaction", "id"), ("Transaction", "status"),
]

# ---- memory_<analyst> schema ------------------------------------------------
# Nodes
#   Investigation(id, question, ts)
#   Finding(id, statement, confidence, status, ts)   status: active | superseded | false_positive | confirmed
#   Entity(id, kind)        <- reference by id to a node in fraud_data
#   Feedback(id, verdict, ts)
# Relationships
#   (Investigation)-[:FOUND]->(Finding)-[:ABOUT]->(Entity)
#   (Feedback)-[:ON]->(Finding)
#   (Finding)-[:SUPERSEDES]->(Finding)
#   (Investigation)-[:FOLLOWS_UP]->(Investigation)
MEMORY_INDEXES = [
    ("Investigation", "id"), ("Finding", "id"), ("Entity", "id"), ("Feedback", "id"),
]

_ANALYST_RE = re.compile(r"^[a-z0-9_]{2,32}$")


def memory_graph_name(analyst_id: str) -> str:
    """Validate the analyst id and return its memory graph name.

    The strict regex matters for security: the id becomes part of a graph name,
    so it must never contain anything but lowercase letters, digits, underscore.
    """
    if not _ANALYST_RE.match(analyst_id or ""):
        raise ValueError("analyst_id must match [a-z0-9_]{2,32}")
    return f"memory_{analyst_id}"


def connect(host: str, port: int, password: str | None = None) -> FalkorDB:
    """Connect to FalkorDB. Local Docker needs only host/port; FalkorDB Cloud also needs a username
    (FALKORDB_USERNAME, usually 'falkordb') and sometimes TLS (FALKORDB_SSL=true)."""
    username = os.getenv("FALKORDB_USERNAME") or None
    ssl = os.getenv("FALKORDB_SSL", "").strip().lower() in ("1", "true", "yes")
    return FalkorDB(host=host, port=port, username=username, password=password or None, ssl=ssl)


def _create_indexes(graph, indexes: list[tuple[str, str]]) -> None:
    for label, prop in indexes:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as exc:  # already indexed -> fine
            if "already indexed" not in str(exc).lower():
                raise


def ensure_data_schema(db: FalkorDB) -> None:
    _create_indexes(db.select_graph(DATA_GRAPH), DATA_INDEXES)


def ensure_memory_schema(db: FalkorDB, analyst_id: str) -> str:
    name = memory_graph_name(analyst_id)
    _create_indexes(db.select_graph(name), MEMORY_INDEXES)
    return name
