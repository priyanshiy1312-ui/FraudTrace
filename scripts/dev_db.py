"""Docker-free FalkorDB for local development and CI (needs `pip install falkordblite`).

    python scripts/dev_db.py            # serves FalkorDB on localhost:6379 until Ctrl+C
    python scripts/dev_db.py 6390       # custom port
"""
import sys
import time

from redislite.falkordb_client import FalkorDB

port = sys.argv[1] if len(sys.argv) > 1 else "6379"
db = FalkorDB("/tmp/fraudtrace-dev.db", serverconfig={"port": port, "bind": "127.0.0.1"})
print(f"FalkorDB (embedded) listening on 127.0.0.1:{port}  - Ctrl+C to stop", flush=True)
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    pass
