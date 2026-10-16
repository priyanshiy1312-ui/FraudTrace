"""The read-only Cypher guard must reject every way of writing, and accept normal reads."""
import pytest

from shared.guard import UnsafeQuery, make_readonly


@pytest.mark.parametrize("q", [
    "MATCH (n) RETURN n LIMIT 5",
    "MATCH (a:Account {id:'A-0001'})-[:SENT]->(t) RETURN t.amount",
    "MATCH p=(a:Account)-[:PAID*2..4]->(b) RETURN p LIMIT 3",
    "MATCH (n) WHERE n.note = 'please DELETE me' RETURN n",         # keyword inside a string is fine
    "MATCH (n) RETURN n.set, n.status LIMIT 2",                       # property named like a keyword
])
def test_allows_reads(q):
    assert "LIMIT" in make_readonly(q).upper()


@pytest.mark.parametrize("q", [
    "CREATE (:X)",
    "MATCH (n) DELETE n",
    "MATCH (n) DETACH DELETE n",
    "MATCH (n) SET n.x = 1 RETURN n",
    "MATCH (n) REMOVE n.x RETURN n",
    "MERGE (n:X {id:1}) RETURN n",
    "CALL db.labels()",
    "MATCH (n) RETURN n; MATCH (m) DELETE m",
    "MATCH (n) /* hidden */ delete n",
    "match (n) dElEtE n",
    "MATCH (n)-[*]->(m) RETURN m",                                    # unbounded path
    "MATCH (n)-[*2..]->(m) RETURN m",
    "MATCH (n) RETURN n // trailing\nDELETE n",
    "x" * 3000,
    "",
    "DROP INDEX x",
])
def test_blocks_writes_and_abuse(q):
    with pytest.raises(UnsafeQuery):
        make_readonly(q)


def test_adds_limit_but_keeps_existing_one():
    assert make_readonly("MATCH (n) RETURN n").endswith("LIMIT 200")
    assert make_readonly("MATCH (n) RETURN n LIMIT 7").endswith("LIMIT 7")
