"""Read-only Cypher guard for the `run_readonly_cypher` tool.

Defence in depth - three layers:
  1. This guard: strips strings/comments, rejects write/procedure keywords,
     multi-statement input and unbounded variable-length paths, and adds a LIMIT.
  2. The query is executed with GRAPH.RO_QUERY, so FalkorDB itself refuses writes.
  3. A per-query timeout, and the query can only ever touch `fraud_data`
     (never a memory_<analyst> graph).
"""
from __future__ import annotations

import re

MAX_QUERY_CHARS = 2000
DEFAULT_LIMIT = 200

FORBIDDEN = {
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "CALL",
    "LOAD", "FOREACH", "INDEX", "CONSTRAINT", "COPY", "EXPLAIN", "PROFILE",
}


class UnsafeQuery(ValueError):
    pass


def _strip_literals(q: str) -> str:
    """Remove comments, string literals and `backtick` names so keywords inside them don't count."""
    out, i, n = [], 0, len(q)
    while i < n:
        c = q[i]
        if q.startswith("//", i):
            j = q.find("\n", i)
            i = n if j == -1 else j
        elif q.startswith("/*", i):
            j = q.find("*/", i + 2)
            if j == -1:
                raise UnsafeQuery("unterminated comment")
            i = j + 2
        elif c in "'\"`":
            j = i + 1
            while j < n and q[j] != c:
                j += 2 if q[j] == "\\" else 1
            if j >= n:
                raise UnsafeQuery("unterminated string literal")
            out.append(" ")          # keep token boundaries
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def make_readonly(query: str) -> str:
    """Return the query (with a LIMIT added if missing) or raise UnsafeQuery."""
    if not isinstance(query, str) or not query.strip():
        raise UnsafeQuery("empty query")
    if len(query) > MAX_QUERY_CHARS:
        raise UnsafeQuery(f"query longer than {MAX_QUERY_CHARS} characters")
    cleaned = _strip_literals(query)
    if ";" in cleaned:
        raise UnsafeQuery("only a single statement is allowed (no ';')")
    for m in re.finditer(r"(?<![.\w$])([A-Za-z_]+)", cleaned):   # skip n.set / $param
        if m.group(1).upper() in FORBIDDEN:
            raise UnsafeQuery(f"keyword '{m.group(1).upper()}' is not allowed in read-only mode")
    if not re.search(r"\bMATCH\b|\bRETURN\b|\bUNWIND\b", cleaned, re.I):
        raise UnsafeQuery("query must be a MATCH/RETURN read query")
    if re.search(r"\*\s*(\d*\s*\.\.\s*)?\]", cleaned):
        raise UnsafeQuery("unbounded variable-length paths are not allowed; use e.g. [*1..4]")
    q = query.strip()
    if not re.search(r"\bLIMIT\b", cleaned, re.I):
        q += f" LIMIT {DEFAULT_LIMIT}"
    return q
