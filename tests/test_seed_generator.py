"""Sanity checks on the synthetic data (no database required)."""
from collections import Counter

import pytest

from seed.generate import FLAGGED_TX_ID, N_ACCOUNTS, N_TX, generate


@pytest.fixture(scope="module")
def ds():
    return generate()


def test_sizes(ds):
    assert len(ds.accounts) == N_ACCOUNTS
    assert len(ds.transactions) == N_TX


def test_tx_ids_unique_and_demo_tx_exists(ds):
    ids = [t["id"] for t in ds.transactions]
    assert len(set(ids)) == len(ids)
    flagged = next(t for t in ds.transactions if t["id"] == FLAGGED_TX_ID)
    assert flagged["status"] == "flagged"
    assert flagged["src"] in ds.ground_truth["ring"]["accounts"]


def test_edges_reference_real_nodes(ds):
    acc = {a["id"] for a in ds.accounts}
    dev = {d["id"] for d in ds.devices}
    ips = {i["id"] for i in ds.ips}
    mer = {m["id"] for m in ds.merchants}
    assert all(e["src"] in acc and e["dst"] in dev for e in ds.used_device)
    assert all(e["src"] in acc and e["dst"] in ips for e in ds.logged_in_from)
    for t in ds.transactions:
        assert t["src"] in acc
        assert t["dst"] in (acc if t["dst_label"] == "Account" else mer)


def test_ring_shares_devices_and_ips_and_loops(ds):
    gt = ds.ground_truth["ring"]
    ring = set(gt["accounts"])
    dev_users = Counter(e["dst"] for e in ds.used_device if e["src"] in ring)
    assert set(dev_users) == set(gt["devices"])
    ip_users = {e["dst"] for e in ds.logged_in_from if e["src"] in ring}
    assert ip_users == set(gt["ips"])
    loop = [t for t in ds.transactions if t["src"] in ring and t["dst"] in ring]
    assert len(loop) == 72   # 6 laps x 12 accounts, one full cycle per lap


def test_mules_collect_then_cash_out(ds):
    gt = ds.ground_truth["mule"]
    for m in gt["accounts"]:
        inbound = [t for t in ds.transactions if t["dst"] == m and t["amount"] <= 200 and t["src"] not in gt["accounts"]]
        outbound = [t for t in ds.transactions if t["src"] == m and t["dst"] == gt["cashout_account"]]
        assert len(inbound) >= 25 and len(outbound) == 1


def test_synthetic_identities_share_device_and_address_but_not_names(ds):
    gt = ds.ground_truth["synthetic_identity"]
    owner = {e["dst"]: e["src"] for e in ds.owns}
    persons = {p["id"]: p for p in ds.persons}
    people = [persons[owner[a]] for a in gt["accounts"]]
    assert len({p["address"] for p in people}) == 1
    assert len({p["name"] for p in people}) == len(people)
    assert {e["dst"] for e in ds.used_device if e["src"] in gt["accounts"]} == {gt["device"]}


def test_deterministic():
    assert generate(1).transactions[:50] == generate(1).transactions[:50]
