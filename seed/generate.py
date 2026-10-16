"""Synthetic data generator for FraudTrace.

Pure Python (no database needed) so it can be unit-tested. It builds ~5k accounts
and ~50k transactions of realistic noise, then plants three fraud patterns.
The answer key is written to a SEPARATE ground_truth.json; it is never stored in
the graph, so the agent has to discover the patterns for real.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SEED = 42
N_ACCOUNTS = 5000
N_TX = 50_000
N_MERCHANTS = 200
N_NORMAL_DEVICES = 4200
N_NORMAL_IPS = 3000
FLAGGED_TX_ID = "T-9001"          # the transaction used in the demo
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # fixed -> reproducible

FIRST = ["Aarav", "Priya", "Rohan", "Sneha", "Vikram", "Anjali", "Karan", "Neha", "Arjun", "Meera",
         "Rahul", "Divya", "Aditya", "Pooja", "Sanjay", "Kavya", "Manish", "Ritu", "Amit", "Isha",
         "Nikhil", "Tara", "Deepak", "Simran", "Varun", "Lata", "Harsh", "Nisha", "Gaurav", "Swati"]
LAST = ["Sharma", "Verma", "Gupta", "Singh", "Patel", "Mehta", "Joshi", "Rathore", "Yadav", "Nair",
        "Reddy", "Iyer", "Khan", "Das", "Bose", "Chauhan", "Mishra", "Saxena", "Kapoor", "Malhotra"]
STREETS = ["MG Road", "Nehru Marg", "Gandhi Nagar", "Station Road", "Lake View", "Civil Lines",
           "Ashok Vihar", "Sector 14", "Park Street", "Hill Road", "Temple Lane", "Market Road"]
CITIES = ["Jaipur", "Jodhpur", "Udaipur", "Delhi", "Pune", "Mumbai", "Indore", "Lucknow"]
MERCHANT_A = ["Blue", "Urban", "Daily", "Prime", "Green", "Swift", "Royal", "Metro", "Nova", "Zen"]
MERCHANT_B = ["Mart", "Cafe", "Travels", "Pharmacy", "Electronics", "Fashion", "Fuel", "Books", "Grocers", "Gadgets"]
CATEGORIES = ["grocery", "food", "travel", "health", "electronics", "fashion", "fuel", "education", "gaming"]
DEVICE_KINDS = ["mobile", "mobile", "mobile", "desktop", "tablet"]
OS_LIST = ["Android", "iOS", "Windows", "macOS"]


@dataclass
class Dataset:
    persons: list[dict] = field(default_factory=list)
    accounts: list[dict] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    ips: list[dict] = field(default_factory=list)
    merchants: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)   # id, amount, ts, status, src, dst, dst_label
    owns: list[dict] = field(default_factory=list)           # {src: person, dst: account}
    used_device: list[dict] = field(default_factory=list)    # {src: account, dst: device}
    logged_in_from: list[dict] = field(default_factory=list)  # {src: account, dst: ip}
    ground_truth: dict = field(default_factory=dict)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(seed: int = SEED, n_accounts: int = N_ACCOUNTS, n_tx: int = N_TX) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset()

    # ---- merchants -----------------------------------------------------
    for i in range(1, N_MERCHANTS + 1):
        ds.merchants.append({
            "id": f"M-{i:03d}",
            "name": f"{rng.choice(MERCHANT_A)} {rng.choice(MERCHANT_B)} {i}",
            "category": rng.choice(CATEGORIES),
        })
    merchant_ids = [m["id"] for m in ds.merchants]
    merchant_weights = [1 / (i + 1) ** 0.8 for i in range(N_MERCHANTS)]  # a few popular, many rare

    # ---- persons and accounts (one account per person) -------------------
    account_ids = [f"A-{i:04d}" for i in range(1, n_accounts + 1)]
    shared_homes: list[str] = []
    for i, acc_id in enumerate(account_ids, start=1):
        # ~3% of people share a household address: realistic false-positive noise
        if shared_homes and rng.random() < 0.03:
            address = rng.choice(shared_homes)
        else:
            address = f"{rng.randint(1, 400)} {rng.choice(STREETS)}, {rng.choice(CITIES)}"
            if rng.random() < 0.1:
                shared_homes.append(address)
        pid = f"P-{i:04d}"
        ds.persons.append({"id": pid, "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}", "address": address})
        ds.accounts.append({
            "id": acc_id,
            "type": rng.choice(["checking", "savings"]),
            "opened": _iso(NOW - timedelta(days=rng.randint(60, 2500))),
            "status": "active",
        })
        ds.owns.append({"src": pid, "dst": acc_id})
    person_of = {a: f"P-{i:04d}" for i, a in enumerate(account_ids, start=1)}
    person_by_id = {p["id"]: p for p in ds.persons}

    # ---- devices and IPs (normal pool) ------------------------------------
    for i in range(1, N_NORMAL_DEVICES + 1):
        ds.devices.append({"id": f"D-{i:04d}", "kind": rng.choice(DEVICE_KINDS), "os": rng.choice(OS_LIST)})
    for i in range(1, N_NORMAL_IPS + 1):
        ds.ips.append({"id": f"IP-{i:04d}", "ip": f"{rng.randint(11, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", "country": "IN"})
    normal_devices = [d["id"] for d in ds.devices]
    normal_ips = [x["id"] for x in ds.ips]

    # ---- choose roles for planted accounts (disjoint) ----------------------
    shuffled = account_ids[:]
    rng.shuffle(shuffled)
    ring = shuffled[:12]
    mules = shuffled[12:15]
    cashout = shuffled[15]
    synth = shuffled[16:22]
    special = set(ring) | set(mules) | {cashout} | set(synth)

    # planted devices / IPs get ordinary-looking ids (no giveaway in the name)
    def add_device() -> str:
        did = f"D-{len(ds.devices) + 1:04d}"
        ds.devices.append({"id": did, "kind": "mobile", "os": "Android"})
        return did

    def add_ip() -> str:
        iid = f"IP-{len(ds.ips) + 1:04d}"
        ds.ips.append({"id": iid, "ip": f"{rng.randint(11, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", "country": "IN"})
        return iid

    ring_devices = [add_device() for _ in range(3)]
    ring_ips = [add_ip() for _ in range(2)]
    synth_device = add_device()

    # ---- device / IP edges ------------------------------------------------
    used: set[tuple[str, str]] = set()
    logged: set[tuple[str, str]] = set()
    for acc in account_ids:
        if acc in ring:
            i = ring.index(acc)
            used.add((acc, ring_devices[i % 3]))
            if i % 2 == 0:
                used.add((acc, ring_devices[(i + 1) % 3]))
            for ip in ring_ips:
                logged.add((acc, ip))
        elif acc in synth:
            used.add((acc, synth_device))
            logged.add((acc, rng.choice(normal_ips)))
        else:
            used.add((acc, rng.choice(normal_devices)))
            if rng.random() < 0.15:
                used.add((acc, rng.choice(normal_devices)))
            for _ in range(rng.randint(1, 2)):
                logged.add((acc, rng.choice(normal_ips)))
    ds.used_device = [{"src": a, "dst": d} for a, d in sorted(used)]
    ds.logged_in_from = [{"src": a, "dst": i} for a, i in sorted(logged)]

    # synthetic identities: different names, same address
    shared_addr = "77 Ashok Vihar, Jaipur"
    synth_names = ["Ravi Kulkarni", "Sunita Bhatt", "Imran Qureshi", "Deepa Menon", "Tarun Ahuja", "Leena Dsouza"]
    for acc, nm in zip(synth, synth_names):
        p = person_by_id[person_of[acc]]
        p["name"], p["address"] = nm, shared_addr

    # ---- transactions ------------------------------------------------------
    txs: list[dict] = []

    def tx(src: str, dst: str, dst_label: str, amount: float, ts: datetime, pattern: str | None, status: str = "completed") -> dict:
        t = {"amount": round(amount, 2), "ts": _iso(ts), "status": status,
             "src": src, "dst": dst, "dst_label": dst_label, "pattern": pattern}
        txs.append(t)
        return t

    # (a) fraud ring: money cycles A0->A1->...->A11->A0, six laps, shrinking slightly (layering)
    ring_start = NOW - timedelta(days=6)
    ring_first: dict | None = None
    n = 0
    for lap in range(6):
        for i in range(12):
            amt = 9800 * (0.99 ** n)
            t = tx(ring[i], ring[(i + 1) % 12], "Account", amt,
                   ring_start + timedelta(hours=lap * 10 + i * 0.7), "ring")
            ring_first = ring_first or t
            n += 1
    ring_merchant = merchant_ids[150]   # a rarely-used merchant the ring also pays
    for acc in ring:
        for _ in range(2):
            tx(acc, ring_merchant, "Merchant", rng.uniform(2500, 4000),
               NOW - timedelta(hours=rng.randint(1, 140)), "ring")
    assert ring_first is not None
    ring_first["status"] = "flagged"

    # (b) mule accounts: many small inbound transfers, then one fast large outbound
    normal_accounts = [a for a in account_ids if a not in special]
    for m in mules:
        n_in = rng.randint(25, 35)
        times = sorted(NOW - timedelta(hours=rng.uniform(2, 120)) for _ in range(n_in))
        total = 0.0
        for ts in times:
            amt = rng.uniform(20, 200)
            total += amt
            tx(rng.choice(normal_accounts), m, "Account", amt, ts, "mule")
        tx(m, cashout, "Account", total * 0.95, times[-1] + timedelta(minutes=40), "mule")

    # (c) synthetic identities: short, fast spending sprees at merchants
    for acc in synth:
        for _ in range(rng.randint(8, 12)):
            tx(acc, rng.choice(merchant_ids), "Merchant", rng.uniform(200, 1500),
               NOW - timedelta(hours=rng.randint(1, 200)), "synthetic_identity")

    # noise: fill up to N_TX with ordinary activity (ring/mule accounts get camouflage traffic too)
    planted_count = len(txs)
    for _ in range(n_tx - planted_count):
        src = rng.choice(account_ids)
        ts = NOW - timedelta(seconds=rng.randint(0, 90 * 86400))
        amt = min(max(rng.lognormvariate(4.0, 1.0), 1.0), 5000.0)
        status = "flagged" if rng.random() < 0.0008 else "completed"   # ~40 noisy flags
        if rng.random() < 0.75:
            dst = rng.choices(merchant_ids, weights=merchant_weights)[0]
            tx(src, dst, "Merchant", amt, ts, None, status)
        else:
            dst = rng.choice(account_ids)
            while dst == src:
                dst = rng.choice(account_ids)
            tx(src, dst, "Account", amt, ts, None, status)

    # ---- assign ids: T-9001 is reserved for the first ring transaction ---------
    pool = [k for k in range(1, n_tx + 2) if k != 9001]
    rng.shuffle(pool)
    others = iter(pool)
    for t in txs:
        t["id"] = FLAGGED_TX_ID if t is ring_first else f"T-{next(others)}"
    ds.transactions = [{k: v for k, v in t.items() if k != "pattern"} for t in txs]

    # ---- answer key (kept OUT of the graph) ---------------------------------
    by_pattern: dict[str, list[str]] = {}
    for t in txs:
        if t["pattern"]:
            by_pattern.setdefault(t["pattern"], []).append(t["id"])
    ds.ground_truth = {
        "ring": {"accounts": sorted(ring), "devices": ring_devices, "ips": ring_ips,
                 "merchant": ring_merchant, "flagged_transaction": FLAGGED_TX_ID,
                 "transactions": by_pattern["ring"]},
        "mule": {"accounts": sorted(mules), "cashout_account": cashout, "transactions": by_pattern["mule"]},
        "synthetic_identity": {"accounts": sorted(synth), "device": synth_device,
                               "address": shared_addr, "transactions": by_pattern["synthetic_identity"]},
    }
    return ds
