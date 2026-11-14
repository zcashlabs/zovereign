#!/usr/bin/env python3
"""Exercise bootstrap failure paths and the complete supervisor with small fixtures."""

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


MODULE = Path(os.environ.get("STACK_MODULE", Path(__file__).resolve().parents[1] / "docker/stack.py"))
spec = importlib.util.spec_from_file_location("stack", MODULE)
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, data in entries.items():
            entry = tarfile.TarInfo(name)
            if isinstance(data, tarfile.TarInfo):
                entry = data
                output.addfile(entry)
            else:
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
    return subprocess.check_output(["zstd", "-q", "-c"], input=stream.getvalue())


@contextmanager
def publisher():
    state = {"files": {}, "requests": [], "truncate_once": False, "fail": False, "wrong_range": False, "ignore_range": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Range")))
            if state["fail"]:
                self.send_error(503)
                return
            value = state["files"].get(self.path)
            if value is None:
                self.send_error(404)
                return
            if hook := state.get("before_response"):
                hook(self.headers.get("Range"))
            total = len(value)
            offset, end = 0, total - 1
            byte_range = self.headers.get("Range")
            if state["ignore_range"]:
                byte_range = None
            if byte_range:
                offset, end = map(int, byte_range.removeprefix("bytes=").split("-"))
                end = min(end, total - 1)
            body = value[offset:end + 1]
            self.send_response(206 if byte_range else 200)
            if byte_range:
                returned_offset = offset + 1 if state["wrong_range"] else offset
                self.send_header("Content-Range", f"bytes {returned_offset}-{end}/{total}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if byte_range and state["truncate_once"] and offset == state.get("truncate_offset", offset):
                state["truncate_once"] = False
                body = body[:len(body) // 2]
                self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fixtures(server):
    node = archive({"state/v30/mainnet/CURRENT": b"MANIFEST-000001\n",
                    "state/v30/mainnet/MANIFEST-000001": b"manifest",
                    "state/v30/mainnet/000001.sst": os.urandom(8192)})
    light = archive({"./db/main/blocks": os.urandom(8192), "./db/main/lengths": b"lengths"})
    node_info = {"network": "mainnet", "snapshot_kind": "archive", "roles": ["latest"],
                 "zakura_version": "1.8.7", "db_major": 30, "size_bytes": len(node),
                 "sha256": hashlib.sha256(node).hexdigest(), "url": server["url"] + "/node.tar.zst"}
    light_info = {"format_version": 1, "network": "mainnet", "lightwalletd_version": "v0.6.0",
                  "size": len(light), "sha256": hashlib.sha256(light).hexdigest(),
                  "snapshot_url": server["url"] + "/light.tar.zst"}
    server["files"].update({"/node.tar.zst": node, "/light.tar.zst": light,
                            "/node.json": json.dumps([node_info]).encode(),
                            "/light.json": json.dumps(light_info).encode()})


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "bootstrap").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_both_snapshots_restore_and_restart_offline(self):
        with publisher() as server:
            fixtures(server)
            for kind, name in (("zakura", "node"), ("lightwalletd", "light")):
                stack.bootstrap(kind, self.root, server["url"] + f"/{name}.json", 2048, 0)
                self.assertTrue(stack.valid_database(kind, self.root / kind))
                self.assertTrue((self.root / kind / ".bootstrap.json").is_file())
                self.assertFalse((self.root / "bootstrap" / f"{kind}.tar.zst.part").exists())
            count = len(server["requests"])
            server["fail"] = True
            for kind, name in (("zakura", "node"), ("lightwalletd", "light")):
                stack.bootstrap(kind, self.root, server["url"] + f"/{name}.json", 2048, 0)
            self.assertEqual(len(server["requests"]), count)

    def test_resumes_pinned_download_and_retries_truncated_ranges(self):
        with publisher() as server, patch.object(stack.time, "sleep"), patch.dict(os.environ, {"DOWNLOAD_WORKERS": "1"}):
            fixtures(server)
            manifest = server["url"] + "/node.json"
            item = stack.choose_snapshot("zakura", manifest)
            cache = self.root / "bootstrap"
            stack.write_json(cache / "zakura.json", item)
            (cache / "zakura.tar.zst.part").write_bytes(server["files"]["/node.tar.zst"][:2048])
            server["files"]["/node.json"] = b"[]"
            server["requests"].clear()
            server["truncate_once"] = True
            stack.bootstrap("zakura", self.root, manifest, 2048, 0)
            ranges = [value for path, value in server["requests"] if path == "/node.tar.zst"]
            self.assertEqual(ranges[0], "bytes=2048-4095")
            self.assertEqual(ranges[1], ranges[0])
            self.assertNotIn(("/node.json", None), server["requests"])
            self.assertTrue(stack.valid_database("zakura", self.root / "zakura"))

    def test_wrong_checksum_never_installs_database(self):
        with publisher() as server:
            fixtures(server)
            manifest = json.loads(server["files"]["/light.json"])
            manifest["sha256"] = "0" * 64
            server["files"]["/light.json"] = json.dumps(manifest).encode()
            with self.assertRaisesRegex(stack.StackError, "checksum mismatch"):
                stack.bootstrap("lightwalletd", self.root, server["url"] + "/light.json", 65536, 0)
            self.assertFalse((self.root / "lightwalletd").exists())
            self.assertFalse((self.root / "bootstrap/lightwalletd.tar.zst.part").exists())

    def test_parallel_ranges_commit_in_order_and_clean_prefetch_files(self):
        with publisher() as server:
            fixtures(server)
            item = stack.choose_snapshot("zakura", server["url"] + "/node.json")
            source = self.root / "bootstrap/zakura.tar.zst.part"
            stack.download(item, source, 1024, 0, workers=3)
            self.assertEqual(source.read_bytes(), server["files"]["/node.tar.zst"])
            self.assertEqual(list(source.parent.glob(source.name + ".range-*")), [])
            self.assertEqual(len(stack.CHILDREN), 0)

    def test_slow_first_range_refills_slots_with_bounded_prefetch(self):
        with publisher() as server:
            data = bytes(range(256)) * 64
            server["files"]["/data"] = data
            first_started, refilled, release = (threading.Event() for _ in range(3))

            def gate(byte_range):
                start = int(byte_range.split("=")[1].split("-")[0])
                if start == 0:
                    first_started.set()
                    release.wait(10)
                elif start >= 3 * 1024:
                    refilled.set()

            server["before_response"] = gate
            item = {"url": server["url"] + "/data", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            source = self.root / "bootstrap/data.part"
            errors = []

            def run():
                try:
                    stack.download(item, source, 1024, 0, workers=3)
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(first_started.wait(3))
                # Old scheduling cannot start range four until the gate opens.
                self.assertTrue(refilled.wait(3), "idle slot was not refilled")
                time.sleep(0.2)
                requests = [r for path, r in server["requests"] if path == "/data"]
                self.assertLessEqual(len(requests), 6, "prefetch exceeded two worker windows")
                self.assertEqual(source.stat().st_size, 0, "committed a noncontiguous prefix")
            finally:
                release.set()
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(list(source.parent.glob(source.name + ".range-*")), [])
            self.assertEqual(len(stack.CHILDREN), 0)

    def test_later_range_retries_while_first_is_blocked(self):
        with publisher() as server:
            data = bytes(range(256)) * 32
            server["files"]["/data"] = data
            server["truncate_once"] = True
            server["truncate_offset"] = 1024
            release, refilled = threading.Event(), threading.Event()
            retries = []

            def gate(byte_range):
                start = int(byte_range.split("=")[1].split("-")[0])
                if start == 0:
                    release.wait(8)
                elif start == 1024:
                    retries.append(start)
                    if len(retries) == 2:
                        release.set()
                elif start >= 3 * 1024:
                    refilled.set()

            server["before_response"] = gate
            item = {"url": server["url"] + "/data", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            source = self.root / "bootstrap/retry.part"
            try:
                stack.download(item, source, 1024, 0, workers=3)
            finally:
                release.set()
            self.assertEqual(len(retries), 2)
            self.assertTrue(refilled.is_set())
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(len(stack.CHILDREN), 0)
            self.assertEqual(list(source.parent.glob(source.name + ".range-*")), [])

    def test_waiter_start_failure_cleans_spawned_download(self):
        with publisher() as server:
            data = bytes(range(256)) * 32
            server["files"]["/data"] = data
            item = {"url": server["url"] + "/data", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            source = self.root / "bootstrap/thread-failure.part"
            source.write_bytes(data[:1500])
            original_start = threading.Thread.start

            def fail_waiter(thread):
                if thread._target.__name__ == "notify_exit":
                    raise RuntimeError("injected waiter failure")
                return original_start(thread)

            with patch.object(stack.threading.Thread, "start", fail_waiter):
                with self.assertRaisesRegex(RuntimeError, "injected waiter failure"):
                    stack.download(item, source, 1024, 0, workers=3)
            self.assertEqual(source.read_bytes(), data[:1500])
            self.assertEqual(len(stack.CHILDREN), 0)
            self.assertEqual(list(source.parent.glob(source.name + ".range-*")), [])

    def test_parallel_shutdown_preserves_prefix_and_cleans_children(self):
        with publisher() as server:
            data = bytes(range(256)) * 32
            server["files"]["/data"] = data
            release = threading.Event()
            server["before_response"] = lambda _range: release.wait(5)
            item = {"url": server["url"] + "/data", "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            source = self.root / "bootstrap/cancel.part"
            source.write_bytes(data[:1500])
            original_spawn = stack.spawn
            interrupted = False

            def interrupt_once(*args, **kwargs):
                process = original_spawn(*args, **kwargs)
                original_poll = process.poll

                def poll():
                    nonlocal interrupted
                    if not interrupted:
                        interrupted = True
                        raise stack.Shutdown()
                    return original_poll()

                process.poll = poll
                return process

            try:
                with patch.object(stack, "spawn", side_effect=interrupt_once):
                    with self.assertRaises(stack.Shutdown):
                        stack.download(item, source, 1024, 0, workers=3)
            finally:
                release.set()
            self.assertEqual(source.read_bytes(), data[:1500])
            self.assertEqual(len(stack.CHILDREN), 0)
            self.assertEqual(list(source.parent.glob(source.name + ".range-*")), [])

    def test_parallel_resume_keeps_existing_prefix(self):
        with publisher() as server:
            fixtures(server)
            item = stack.choose_snapshot("zakura", server["url"] + "/node.json")
            data = server["files"]["/node.tar.zst"]
            source = self.root / "bootstrap/resume.part"
            source.write_bytes(data[:1500])
            stack.download(item, source, 1024, 0, workers=3)
            self.assertEqual(source.read_bytes(), data)
            starts = [int(r.split("=")[1].split("-")[0])
                      for path, r in server["requests"] if path == "/node.tar.zst"]
            self.assertEqual(min(starts), 1500)

    def test_incorrect_ranges_never_append_to_cached_archive(self):
        with publisher() as server, patch.object(stack.time, "sleep"):
            fixtures(server)
            item = stack.choose_snapshot("zakura", server["url"] + "/node.json")
            source = self.root / "bootstrap/zakura.tar.zst.part"
            prefix = server["files"]["/node.tar.zst"][:2048]
            source.write_bytes(prefix)
            server["wrong_range"] = True
            with self.assertRaisesRegex(stack.StackError, "incorrect HTTP byte range"):
                stack.download(item, source, 2048, 0)
            self.assertEqual(source.read_bytes(), prefix)

    def test_disk_space_failure_prevents_archive_download(self):
        with publisher() as server:
            fixtures(server)
            item = stack.choose_snapshot("zakura", server["url"] + "/node.json")
            with patch.object(stack.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()):
                with self.assertRaisesRegex(stack.StackError, "Insufficient free disk"):
                    stack.download(item, self.root / "bootstrap/zakura.tar.zst.part", 2048, 0)
            self.assertFalse(any(path == "/node.tar.zst" for path, _ in server["requests"]))

    def test_server_ignoring_ranges_is_bounded_and_prefetch_is_cleaned(self):
        with publisher() as server, patch.object(stack.time, "sleep"):
            fixtures(server)
            item = stack.choose_snapshot("zakura", server["url"] + "/node.json")
            source = self.root / "bootstrap/zakura.tar.zst.part"
            server["ignore_range"] = True
            with self.assertRaises(stack.StackError):
                stack.download(item, source, 1024, 0, workers=3)
            self.assertEqual(source.stat().st_size, 0)
            self.assertEqual(list(source.parent.glob(source.name + ".range-*")), [])
            self.assertEqual(len(stack.CHILDREN), 0)

    def test_version_and_network_mismatches_fail_before_archive_download(self):
        with publisher() as server:
            fixtures(server)
            for field, bad in (("lightwalletd_version", "v0.5.4"), ("network", "testnet")):
                manifest = json.loads(server["files"]["/light.json"])
                manifest[field] = bad
                server["files"]["/invalid.json"] = json.dumps(manifest).encode()
                with self.assertRaises(stack.StackError):
                    stack.choose_snapshot("lightwalletd", server["url"] + "/invalid.json")
            manifest = json.loads(server["files"]["/node.json"])
            manifest[0]["db_major"] = 31
            server["files"]["/invalid.json"] = json.dumps(manifest).encode()
            with self.assertRaisesRegex(stack.StackError, "database major"):
                stack.choose_snapshot("zakura", server["url"] + "/invalid.json")
            self.assertTrue(all(value is None for _, value in server["requests"]))

    def test_existing_unmanaged_data_is_preserved(self):
        target = self.root / "lightwalletd"
        target.mkdir()
        existing = target / "keep-me"
        existing.write_text("valuable data")
        with self.assertRaisesRegex(stack.StackError, "refusing to overwrite"):
            stack.bootstrap("lightwalletd", self.root, "http://unused", 4096, 0)
        self.assertEqual(existing.read_text(), "valuable data")

    def test_unsafe_tar_members_are_rejected(self):
        link = tarfile.TarInfo("db/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../escape"
        for entries in ({"../escape": b"bad"}, {"/tmp/escape": b"bad"}, {"db/link": link}):
            with self.subTest(entries=entries):
                source = self.root / "unsafe.tar.zst"
                source.write_bytes(archive(entries))
                staging = self.root / "staging"
                staging.mkdir(exist_ok=True)
                with self.assertRaises(stack.StackError):
                    stack.extract("lightwalletd", source, staging, 0)
                self.assertFalse((self.root / "escape").exists())

    def test_missing_database_cleans_staging_but_keeps_download(self):
        with publisher() as server:
            fixtures(server)
            body = archive({"db/main/other": b"not a database"})
            manifest = json.loads(server["files"]["/light.json"])
            manifest.update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
            server["files"].update({"/light.tar.zst": body, "/light.json": json.dumps(manifest).encode()})
            with self.assertRaisesRegex(stack.StackError, "expected lightwalletd"):
                stack.bootstrap("lightwalletd", self.root, server["url"] + "/light.json", 65536, 0)
            self.assertFalse((self.root / "lightwalletd").exists())
            self.assertFalse((self.root / "bootstrap/lightwalletd.restore").exists())
            self.assertTrue((self.root / "bootstrap/lightwalletd.tar.zst.part").exists())

    def test_health_checks_rpc_and_real_compact_block_method(self):
        info = {"chain": "main", "pruned": False, "blocks": 100, "estimatedheight": 100}
        with patch.object(stack, "blockchain_info", return_value=info), patch.object(stack, "grpc", side_effect=[{"chainName": "main"}, {"height": "99"}]) as grpc:
            self.assertEqual(stack.health(), 99)
            self.assertEqual(grpc.call_args_list[-1].args, ("GetBlock", {"height": 99}))
        with patch.object(stack, "blockchain_info", return_value={**info, "estimatedheight": 200}), patch.object(stack, "grpc") as grpc:
            with self.assertRaisesRegex(stack.StackError, "catching up"):
                stack.health()
            grpc.assert_not_called()

    def test_local_tls_health_probe_uses_tls_transport(self):
        with patch.dict(os.environ, {"TLS_CERT": "/tls/fullchain.pem"}), patch.object(stack, "command", return_value=b'{"chainName":"main"}') as command:
            self.assertEqual(stack.grpc("GetLightdInfo")["chainName"], "main")
            args = command.call_args.args[0]
            self.assertIn("-insecure", args)
            self.assertNotIn("-plaintext", args)


FAKE_SERVICE = '''#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, os, pathlib, signal, sys, time
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['DATA_DIR'])
def event(kind):
    with (root / 'events').open('a') as out:
        out.write(json.dumps({'name':name,'kind':kind,'pid':os.getpid(),'args':sys.argv[1:],
                              'rpc':os.environ.get('ZAKURA_RPC__LISTEN_ADDR'),'uid':os.getuid()})+'\\n')
def shutdown(*_args):
    event('stop')
    sys.exit(0)
signal.signal(signal.SIGTERM, shutdown)
event('start')
if name == 'zakurad':
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body=json.dumps({'result':{'chain':'main','pruned':False,'blocks':100,'estimatedheight':100}}).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*_args): pass
    HTTPServer(('127.0.0.1',8232),Handler).serve_forever()
else:
    while True: time.sleep(1)
'''


@unittest.skipUnless(shutil.which("zakurad"), "requires the real Zakura binary in the runtime image")
class RealBinaryTests(unittest.TestCase):
    def test_wrapper_environment_does_not_break_zakura_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "offline.toml"
            config.write_text("[network]\ninitial_mainnet_peers = []\n")
            output_path = root / "node.log"
            with patch.dict(os.environ, {"ZAKURA_DB_MAJOR": "30", "ZAKURA_VERSION": "1.8.7", "ZAKURA_SNAPSHOT_MANIFEST": "http://example.invalid/snapshots.json"}):
                env = stack.zakura_environment(root)
            with output_path.open("w") as output:
                process = subprocess.Popen(["zakurad", "--config", str(config), "start"], env=env,
                                           stdout=output, stderr=subprocess.STDOUT)
                try:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        self.fail(output_path.read_text())
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            self.assertNotIn("Configuration error", output_path.read_text())

    @unittest.skipUnless(shutil.which("lightwalletd") and os.getuid() == 0, "requires runtime image and root to select UID 10001")
    def test_real_lightwalletd_can_log_after_privilege_drop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            data = root / "data"
            data.mkdir()
            os.chown(data, 10001, 10001)
            code = '''
import sys, time
sys.path.insert(0, sys.argv[1])
import stack
process = stack.start_lightwalletd([
    'lightwalletd', '--darkside-very-insecure', '--no-tls-very-insecure',
    '--grpc-bind-addr=127.0.0.1:0', '--http-bind-addr=127.0.0.1:0',
    '--data-dir=' + sys.argv[2], '--log-file=/dev/stdout', '--log-level=4'])
try:
    time.sleep(3)
    assert process.poll() is None, 'real lightwalletd exited during startup'
finally:
    stack.stop(process, grace=5)
    for thread in stack.LOG_THREADS: thread.join(timeout=2)
'''
            result = subprocess.run([sys.executable, "-c", code, str(MODULE.parent), str(data)],
                                    user=10001, group=10001, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout.decode())
            self.assertIn(b"Starting lightwalletd process", result.stdout)


class SupervisorTests(unittest.TestCase):
    def test_bootstrap_shutdown_restart_and_child_failure(self):
        with tempfile.TemporaryDirectory() as temporary, publisher() as server:
            fixtures(server)
            base = Path(temporary)
            base.chmod(0o755)
            binaries = base / "bin"
            binaries.mkdir()
            for name in ("zakurad", "lightwalletd"):
                path = binaries / name
                path.write_text(FAKE_SERVICE)
                path.chmod(0o755)
            data = base / "data"
            env = {**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"], "DATA_DIR": str(data),
                   "ZAKURA_SNAPSHOT_MANIFEST": server["url"] + "/node.json",
                   "LIGHTWALLETD_SNAPSHOT_MANIFEST": server["url"] + "/light.json",
                   "DISK_RESERVE_GIB": "0", "RPC_READY_TIMEOUT": "20"}
            log_file = base / "output.log"

            def events():
                path = data / "events"
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

            def wait_for(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.1)
                self.fail(log_file.read_text())

            with log_file.open("w") as output:
                process = subprocess.Popen([sys.executable, str(MODULE), "run"], env=env, stdout=output, stderr=output)
                try:
                    wait_for(lambda: any(item["name"] == "lightwalletd" and item["kind"] == "start" for item in events()))
                    started = [item for item in events() if item["kind"] == "start"]
                    self.assertEqual([item["name"] for item in started], ["zakurad", "lightwalletd"])
                    self.assertEqual(started[0]["rpc"], "127.0.0.1:8232")
                    self.assertNotEqual(started[0]["uid"], 0)
                    second = subprocess.run([sys.executable, str(MODULE), "run"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                    self.assertEqual(second.returncode, 1)
                    self.assertIn(b"Another stack is already using this data volume", second.stdout)
                    process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 0)
                    self.assertEqual(sum(item["kind"] == "stop" for item in events()), 2)
                    requests = len(server["requests"])
                    server["fail"] = True
                    process = subprocess.Popen([sys.executable, str(MODULE), "run"], env=env, stdout=output, stderr=output)
                    wait_for(lambda: sum(item["name"] == "lightwalletd" and item["kind"] == "start" for item in events()) == 2)
                    self.assertEqual(len(server["requests"]), requests)
                    child = [item for item in events() if item["name"] == "lightwalletd" and item["kind"] == "start"][-1]
                    os.kill(child["pid"], signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 1)
                    self.assertEqual(sum(item["name"] == "zakurad" and item["kind"] == "stop" for item in events()), 2)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
