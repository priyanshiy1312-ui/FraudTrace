"""Integration tests: every tool against the real seeded graph, plus memory, actions and tenant isolation."""
import pytest

from mcp_server import tools as t

pytestmark = pytest.mark.usefixtures("live_db")


def test_transaction_links_finds_the_ring(gt):
    d = t.get_transaction_links("T-9001")["data"]
    assert d["sender"] in gt["ring"]["accounts"]
    linked = {x["account"] for x in d["accounts_identity_linked_to_sender"]}
    assert len(linked & set(gt["ring"]["accounts"])) >= 8


def test_communities_recover_ring_and_synthetic_identities(gt):
    comps = [set(c["accounts"]) for c in t.detect_communities()["data"]["communities"]]
    assert set(gt["ring"]["accounts"]) in comps
    assert set(gt["synthetic_identity"]["accounts"]) in comps


def test_cycle_detection_finds_the_12_hop_loop(gt):
    cyc = t.detect_cycles(gt["ring"]["accounts"][0], max_len=12, min_repeats=3)["data"]["cycles"]
    assert cyc and cyc[0]["length"] == 12 and set(cyc[0]["path"]) == set(gt["ring"]["accounts"])


def test_no_cycle_for_ordinary_account():
    assert t.detect_cycles("A-0001")["data"]["cycles"] == []


def test_mule_candidates_match_ground_truth(gt):
    found = {c["account"] for c in t.find_mule_candidates()["data"]["candidates"]}
    assert found == set(gt["mule"]["accounts"])


def test_shared_entities_and_profile(gt):
    acc = gt["ring"]["accounts"][0]
    assert t.find_shared_entities(acc)["data"]["shared_devices"]
    assert t.get_account_profile(acc)["data"]["linked_accounts"]


def test_shortest_path_and_neighbors(gt):
    a, b = gt["ring"]["accounts"][:2]
    sp = t.shortest_path(a, b)["data"]
    assert sp["connected"] and sp["hops"] <= 4
    assert t.get_neighbors(gt["ring"]["devices"][0])["data"]["neighbors"]


def test_rank_suspicious(gt):
    top = {r["account"] for r in t.rank_suspicious_nodes(12)["data"]["ranking"]}
    assert top & (set(gt["ring"]["accounts"]) | set(gt["synthetic_identity"]["accounts"]))


def test_bad_input_is_rejected():
    with pytest.raises(t.ToolError):
        t.get_account_profile("not-an-id")
    with pytest.raises(t.ToolError):
        t.get_neighbors("A-0001", relationship="DROP_TABLE")
    with pytest.raises(t.ToolError):
        t.run_readonly_cypher("MATCH (n) DETACH DELETE n")


def test_scores_separate_planted_from_normal(gt):
    s = lambda a: t.score_account("zz_test_a", a)["data"]["base_score"]
    assert s(gt["ring"]["accounts"][0]) >= 60
    assert s(gt["mule"]["accounts"][0]) >= 30
    assert s("A-0001") == 0


# ------------------------------------------------------------------ memory
def test_memory_save_recall_supersede_feedback_and_score(analysts, gt):
    a, _ = analysts
    acc = gt["ring"]["accounts"][0]
    base = t.score_account(a, acc)["data"]["final_score"]

    inv1 = t.start_investigation(a, "first look")
    f1 = t.save_finding(a, inv1, [acc], "looks like a ring", 0.7, ["detect_cycles"])["data"]["finding_id"]
    assert [f["finding_id"] for f in t.recall_past_findings(a, acc)["data"]["findings"]] == [f1]

    inv2 = t.start_investigation(a, "second look")           # same entities -> supersedes, keeps history
    f2 = t.save_finding(a, inv2, [acc], "confirmed ring behaviour", 0.9, ["find_shared_entities"])["data"]
    assert f2["superseded"] == [f1]
    assert [f["finding_id"] for f in t.recall_past_findings(a, acc)["data"]["findings"]] == [f2["finding_id"]]
    assert len(t.recall_past_findings(a, acc, include_history=True)["data"]["findings"]) == 2
    assert t.memory_graph(a).query("MATCH (:Investigation)-[:FOLLOWS_UP]->(:Investigation) RETURN count(*)").result_set[0][0] >= 1

    t.record_feedback(a, f2["finding_id"], "false_positive")
    after = t.score_account(a, acc)["data"]
    assert after["final_score"] < base and after["memory_adjustment"] < 0


def test_tenant_isolation(analysts, gt):
    a, b = analysts
    acc = gt["ring"]["accounts"][1]
    t.save_finding(a, t.start_investigation(a, "q"), [acc], "secret note for A", 0.8, ["x"])
    assert t.recall_past_findings(a, acc)["data"]["findings"]
    assert t.recall_past_findings(b, acc)["data"]["findings"] == []
    assert t.list_memory(b)["findings"] == []
    with pytest.raises(ValueError):
        t.memory_graph("alice; DROP")                         # analyst id can't be abused to reach other graphs


# ------------------------------------------------------------------ actions
def test_actions_need_evidence_and_graph_support(analysts, gt):
    a, _ = analysts
    ring, normal = gt["ring"]["accounts"][:3], "A-0001"
    with pytest.raises(t.ToolError):
        t.flag_accounts(a, ring, "r", 0.9, [])                 # no evidence
    res = t.flag_accounts(a, ring + [normal], "ring", 0.9, ["detect_communities"])["data"]
    assert {f["account"] for f in res["flagged"]} == set(ring)
    assert res["refused"][0]["account"] == normal              # no graph evidence -> refused
    assert t.get_account_profile(ring[0])["data"]["status"] == "flagged"

    case = t.create_case(a, ring, "ring case", ["detect_cycles"])["data"]
    assert t.ro(t.data_graph(), "MATCH (:Case {id:$c})-[:INCLUDES]->(x) RETURN count(x)", {"c": case["case_id"]})[0][0] == 3
    with pytest.raises(t.ToolError):
        t.create_case(a, [normal], "nothing here", ["x"])

    assert t.hold_transaction(a, "T-9001", "ring loop", ["cycle"])["data"]["held"] is True
