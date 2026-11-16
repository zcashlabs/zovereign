"""Tiny HTTP service for real-Docker lifecycle tests, not a Zcash implementation.

The entrypoint and version metadata follow the management contract so the real
CLI can adopt and replace it. Health verifies the running HTTP service. A durable
identity and start counter prove volume reuse across different container/image IDs.
"""
import json
import os
from pathlib import Path
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
import uuid

ROOT = Path(os.environ.get("DATA_DIR", "/data"))


def status(root):
    """Return observed fixture state, with an explicit fixture marker."""
    identity = json.loads((root / "identity.json").read_text())
    return {
        "schema_version": 1,
        "test_fixture": True,
        "phase": "ready",
        "revision": os.environ["FIXTURE_REVISION"],
        "identity": identity["identity"],
        "starts": int((root / "starts").read_text()),
    }


def health():
    with urlopen("http://127.0.0.1:9067/health", timeout=2) as response:
        report = json.load(response)
    if report["identity"] != json.loads((ROOT / "identity.json").read_text())["identity"]:
        raise RuntimeError("Service identity does not match its data volume")
    return report


def run():
    ROOT.mkdir(parents=True, exist_ok=True)
    marker = ROOT / "identity.json"
    if not marker.exists():
        marker.write_text(json.dumps({"identity": str(uuid.uuid4())}))
    counter = ROOT / "starts"
    counter.write_text(str((int(counter.read_text()) if counter.exists() else 0) + 1))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps(status(ROOT)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 9067), Handler)

    def shutdown(*_args):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({"event": "fixture_ready", **status(ROOT)}), flush=True)
    with server:
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "run":
        run()
    elif mode == "health":
        print(json.dumps(health()))
    elif mode == "status":
        print(json.dumps(status(ROOT)))
    else:
        raise SystemExit("usage: fixture stack.py run|health|status")
