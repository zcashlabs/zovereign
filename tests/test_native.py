#!/usr/bin/env python3
"""Run existing fixtures plus native launcher and packaged-binary regressions."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_stack


NATIVE_MODULE = Path(os.environ.get("NATIVE_MODULE", Path(__file__).resolve().parents[1] / "nix/native.py"))
# The installed runtime keeps native.py and stack.py together.
sys.path.insert(0, str(test_stack.MODULE.parent))
spec = importlib.util.spec_from_file_location("native", NATIVE_MODULE)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
stack = native.stack


class NativeTests(unittest.TestCase):
    def test_platform_data_defaults_and_explicit_override(self):
        cases = [
            ("linux", {}, "/example/.local/share/zovereign"),
            ("linux", {"XDG_DATA_HOME": "/external/user data"}, "/external/user data/zovereign"),
            ("darwin", {}, "/example/Library/Application Support/zovereign"),
            ("darwin", {"DATA_DIR": "/external/chain data"}, "/external/chain data"),
        ]
        for platform, env, expected in cases:
            with self.subTest(platform=platform, env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(native.sys, "platform", platform), patch.object(native.os, "getuid", return_value=501), \
                    patch.object(native.Path, "home", return_value=Path("/example")):
                native.configure_environment()
                self.assertEqual(os.environ["DATA_DIR"], expected)
                self.assertEqual(os.environ["GRPC_BIND_ADDR"], "127.0.0.1:9067")

    def test_root_is_rejected_without_creating_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "must not exist"
            with patch.dict(os.environ, {"DATA_DIR": str(data)}), patch.object(native.os, "getuid", return_value=0):
                with self.assertRaisesRegex(stack.StackError, "without sudo"):
                    native.configure_environment()
            self.assertFalse(data.exists())

    def test_binary_metadata_is_pinned_and_runtime_settings_are_retained(self):
        with patch.dict(os.environ, {"ZAKURA_DB_MAJOR": "999", "LIGHTWALLETD_VERSION": "other",
                                     "DOWNLOAD_WORKERS": "6", "GRPC_BIND_ADDR": "0.0.0.0:19067"}):
            native.configure_environment()
            self.assertEqual(os.environ["ZAKURA_DB_MAJOR"], "30")
            self.assertEqual(os.environ["LIGHTWALLETD_VERSION"], "v0.6.0")
            self.assertEqual(os.environ["DOWNLOAD_WORKERS"], "6")
            self.assertEqual(os.environ["GRPC_BIND_ADDR"], "0.0.0.0:19067")

    def test_health_does_not_bootstrap_or_create_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "absent"
            with patch.dict(os.environ, {"DATA_DIR": str(data)}), patch.object(sys, "argv", ["native.py", "health"]), \
                    patch.object(stack, "health", return_value=99), patch.object(stack.signal, "signal"), \
                    patch.object(stack, "bootstrap") as bootstrap:
                self.assertEqual(native.main(), 0)
                bootstrap.assert_not_called()
                self.assertFalse(data.exists())

    def test_packaged_help_and_invalid_command_do_not_start_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "absent data"
            env = {**os.environ, "DATA_DIR": str(data), "ZAKURA_SNAPSHOT_MANIFEST": "http://example.invalid"}
            for args, expected in ((["--help"], 0), (["unknown"], 2)):
                result = subprocess.run([os.environ["ZOVEREIGN_BIN"], *args], env=env,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr.decode())
                self.assertFalse(data.exists())

    def test_container_listener_default_and_native_health_port(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stack.grpc_bind_address()[0], "0.0.0.0:9067")
        for listener, probe in (("127.0.0.1:19067", "127.0.0.1:19067"),
                                ("0.0.0.0:29067", "127.0.0.1:29067"),
                                ("[::]:19067", "[::1]:19067")):
            with patch.dict(os.environ, {"GRPC_BIND_ADDR": listener}), \
                    patch.object(stack, "command", return_value=b'{}') as command:
                stack.grpc("GetLightdInfo")
                self.assertEqual(command.call_args.args[0][-2], probe)
        for listener in ("", "localhost", "127.0.0.1:0", "127.0.0.1:65536", "[::1", "http://localhost:9067", "user@host:9067"):
            with self.subTest(listener=listener), patch.dict(os.environ, {"GRPC_BIND_ADDR": listener}):
                with self.assertRaises(stack.StackError):
                    stack.grpc_bind_address()

    def test_process_group_signal_races_only_ignore_exited_children(self):
        for error in (ProcessLookupError, PermissionError):
            with self.subTest(error=error):
                child = Mock(pid=123)
                child.poll.return_value = None
                with patch.object(stack.os, "killpg", side_effect=error):
                    stack.stop(child, grace=5)
                self.assertEqual([call.kwargs["timeout"] for call in child.wait.call_args_list], [1, 5])
                child.wait.side_effect = subprocess.TimeoutExpired("child", 1)
                with patch.object(stack.os, "killpg", side_effect=error):
                    with self.assertRaises(error):
                        stack.signal_group(child, signal.SIGTERM)

    def test_native_lifecycle_uses_current_user_loopback_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temporary, test_stack.publisher() as server:
            test_stack.fixtures(server)
            base = Path(temporary)
            binaries = base / "bin"
            binaries.mkdir()
            for name in ("zakurad", "lightwalletd"):
                path = binaries / name
                path.write_text(test_stack.FAKE_SERVICE)
                path.chmod(0o755)
            data = base / "persistent chain data"
            env = {**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"], "DATA_DIR": str(data),
                   "PYTHONPATH": str(test_stack.MODULE.parent),
                   "ZAKURA_SNAPSHOT_MANIFEST": server["url"] + "/node.json",
                   "LIGHTWALLETD_SNAPSHOT_MANIFEST": server["url"] + "/light.json",
                   "DISK_RESERVE_GIB": "0", "RPC_READY_TIMEOUT": "20"}
            env.pop("GRPC_BIND_ADDR", None)
            log = base / "output.log"

            def events():
                path = data / "events"
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

            def wait_for(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.1)
                self.fail(log.read_text())

            with log.open("w") as output:
                # No command argument must select run.
                command = [sys.executable, str(NATIVE_MODULE)]
                process = subprocess.Popen(command, env=env, stdout=output, stderr=output)
                try:
                    wait_for(lambda: any(e["name"] == "lightwalletd" for e in events()))
                    started = [e for e in events() if e["kind"] == "start"]
                    self.assertEqual([e["name"] for e in started], ["zakurad", "lightwalletd"])
                    self.assertTrue(all(e["uid"] == os.getuid() for e in started))
                    self.assertEqual(started[0]["rpc"], "127.0.0.1:8232")
                    self.assertIn("--grpc-bind-addr=127.0.0.1:9067", started[1]["args"])
                    second = subprocess.run(command, env=env, capture_output=True, timeout=10)
                    self.assertEqual(second.returncode, 1)
                    self.assertIn(b"Another stack", second.stdout)
                    process.send_signal(signal.SIGINT)
                    self.assertEqual(process.wait(timeout=15), 0)
                    self.assertEqual(sum(e["kind"] == "stop" for e in events()), 2)
                    requests = len(server["requests"])
                    server["fail"] = True
                    process = subprocess.Popen(command, env=env, stdout=output, stderr=output)
                    wait_for(lambda: sum(e["name"] == "lightwalletd" and e["kind"] == "start" for e in events()) == 2)
                    self.assertEqual(len(server["requests"]), requests)
                    node = [e for e in events() if e["name"] == "zakurad" and e["kind"] == "start"][-1]
                    os.kill(node["pid"], signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=15), 1)
                    self.assertEqual(sum(e["name"] == "lightwalletd" and e["kind"] == "stop" for e in events()), 2)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)

    def test_real_lightwalletd_logs_as_current_user(self):
        with tempfile.TemporaryDirectory() as temporary:
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
            result = subprocess.run([sys.executable, "-c", code, str(test_stack.MODULE.parent), temporary],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode())
            self.assertIn(b"Starting lightwalletd process", result.stdout)


if __name__ == "__main__":
    for binary, args, version in (("zakurad", ["--version"], "1.8.7"),
                                  ("lightwalletd", ["version"], "v0.6.0"),
                                  ("grpcurl", ["--version"], "1.9.")):
        if not shutil.which(binary):
            sys.exit(f"Missing {binary}; run this suite with nix run .#test")
        result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=10)
        if result.returncode or version not in result.stdout + result.stderr:
            sys.exit(f"Unexpected {binary} version: {result.stdout}{result.stderr}")
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_stack)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(NativeTests))
    sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
