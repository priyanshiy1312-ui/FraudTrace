"""Integration tests need a seeded FalkorDB (docker compose up -d falkordb && docker compose run --rm seed).
They are skipped automatically when it isn't reachable."""
import json
import os
from pathlib import Path

import pytest

GT_PATH = Path(__file__).resolve().parents[1] / "seed" / "out" / "ground_truth.json"


@pytest.fixture(scope="session")
def gt():
    if not GT_PATH.exists():
        pytest.skip("run the seed first (ground_truth.json missing)")
    return json.loads(GT_PATH.read_text())


@pytest.fixture(scope="session")
def live_db(gt):
    from mcp_server import tools
    try:
        tools.db().list_graphs()
        n = tools.ro(tools.data_graph(), "MATCH (a:Account) RETURN count(a)")[0][0]
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable / not seeded: {exc}")
    if n < 1000:
        pytest.skip("fraud_data not seeded")
    return tools.db()


@pytest.fixture()
def analysts(live_db):
    """Two throw-away analysts; their memory graphs and any data-graph writes are cleaned up afterwards."""
    from mcp_server import tools
    names = ("zz_test_a", "zz_test_b")
    yield names
    for n in names:
        try:
            live_db.select_graph(f"memory_{n}").delete()
        except Exception:
            pass
    g = tools.data_graph()
    g.query("MATCH (a:Account) WHERE a.flagged_by IN $n SET a.status = 'active' REMOVE a.flag_reason, a.flag_confidence, a.flagged_by, a.flagged_at", {"n": list(names)})
    g.query("MATCH (t:Transaction) WHERE t.held_by IN $n SET t.status = 'flagged' REMOVE t.hold_reason, t.held_by, t.held_at", {"n": list(names)})
    g.query("MATCH (c:Case) WHERE c.opened_by IN $n DETACH DELETE c", {"n": list(names)})
