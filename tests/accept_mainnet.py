#!/usr/bin/env python3
"""Opt-in acceptance check for a running real-mainnet deployment (no fixtures)."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time


PROBE = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, '/opt/zovereign')
import stack
root = Path(os.environ.get('DATA_DIR', '/data'))
print(json.dumps({
    'node': stack.blockchain_info(),
    'lightwalletd': stack.grpc('GetLightdInfo'),
    'served_compact_block': stack.health(),
    'snapshots': {kind: json.loads((root / kind / '.bootstrap.json').read_text())
                  for kind in ('zakura', 'lightwalletd')},
    'leftover_archives': [str(path) for path in (root / 'bootstrap').iterdir()
                         if '.tar.zst' in path.name or path.name.endswith('.restore')]
}))
"""


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def inspect(container):
    return json.loads(docker("inspect", container))[0]


def wait_healthy(container, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = inspect(container)
        state = item["State"]
        if state["Status"] != "running":
            raise RuntimeError(f"Container is {state['Status']}")
        health = state.get("Health", {}).get("Status", "missing")
        if health == "healthy":
            return item
        recent = docker("logs", "--tail", "1", container)
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {health}; {recent}", flush=True)
        time.sleep(30)
    raise RuntimeError("Timed out waiting for mainnet readiness")


def probe(container):
    result = json.loads(docker("exec", container, "python3", "-c", PROBE))
    if result["leftover_archives"]:
        raise RuntimeError(f"Bootstrap artifacts were not removed: {result['leftover_archives']}")
    if result["node"]["blocks"] < result["snapshots"]["zakura"]["height"]:
        raise RuntimeError("Node tip is below its imported snapshot height")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="zovereign")
    parser.add_argument("--wait", type=int, default=86400, help="Seconds to allow for the initial bootstrap")
    parser.add_argument("--restart", action="store_true", help="Restart the container and prove databases are reused")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    before = wait_healthy(args.container, args.wait)
    if before.get("RestartCount", 0):
        raise RuntimeError("Container had unexpected automatic restarts during bootstrap")
    if before["HostConfig"]["PortBindings"].get("8232/tcp"):
        raise RuntimeError("Node RPC is published outside the container")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "image": before["Image"],
              "container": args.container, "before_restart": probe(args.container)}
    initial_logs = docker("logs", args.container)
    report["cache_load_messages"] = [line for line in initial_logs.splitlines()
                                    if "cache" in line.lower() and "reading" in line.lower()]
    if args.restart:
        since = datetime.now(timezone.utc).isoformat()
        print("Application checks passed; restarting the container to verify persistent state", flush=True)
        docker("restart", "--time", "120", args.container)
        wait_healthy(args.container, 1200)
        report["after_restart"] = probe(args.container)
        if report["after_restart"]["snapshots"] != report["before_restart"]["snapshots"]:
            raise RuntimeError("Snapshot provenance changed across restart")
        logs = docker("logs", "--since", since, args.container)
        if "Downloading " in logs or any(f"Reusing existing {kind} database" not in logs for kind in ("zakura", "lightwalletd")):
            raise RuntimeError("Restart did not reuse both bootstrapped databases")
        report["restart_without_download"] = True
    report["passed"] = True
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered, flush=True)


if __name__ == "__main__":
    main()
