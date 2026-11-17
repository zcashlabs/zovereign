"""Opt-in CLI integration against a real Docker engine and small HTTP containers.

Run with ZOVEREIGN_DOCKER_INTEGRATION=1 after cargo build. Uses the selected
Docker context (or ZOVEREIGN_DOCKER_CONTEXT), never creates a VM, and never
downloads Zcash data. The service is a management-contract fixture, not a node.
"""
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("ZOVEREIGN_TEST_BINARY", ROOT / "target/debug/zovereign"))
RUN_LABEL = "io.zovereign.test-run"


@unittest.skipUnless(os.environ.get("ZOVEREIGN_DOCKER_INTEGRATION") == "1",
                     "opt in with ZOVEREIGN_DOCKER_INTEGRATION=1; requires real Docker")
class DockerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("ZOVEREIGN_DOCKER") or os.environ.get("FAKE_DOCKER_STATE"):
            raise RuntimeError("Unset ZOVEREIGN_DOCKER and FAKE_DOCKER_STATE for real Docker tests")
        if not BINARY.is_file():
            raise RuntimeError(f"Build the CLI with cargo build first: {BINARY}")
        executable = shutil.which("docker")
        if not executable:
            raise RuntimeError("Install the Docker CLI and start your existing Docker engine")
        cls.env = dict(os.environ)
        cls.context = os.environ.get("ZOVEREIGN_DOCKER_CONTEXT")
        if not cls.context:
            if os.environ.get("DOCKER_HOST"):
                raise RuntimeError("Set ZOVEREIGN_DOCKER_CONTEXT explicitly when DOCKER_HOST is set")
            cls.context = cls.execute([executable, "context", "show"]).stdout.strip()
        # Pin all fixture and CLI commands to the same engine, including cleanup.
        cls.docker_command = [executable, "--context", cls.context]
        info = json.loads(cls.docker("info", "--format", "{{json .}}").stdout)
        if info["OSType"] != "linux":
            raise RuntimeError("The integration fixtures require a Linux Docker engine")
        cls.run_id = uuid.uuid4().hex[:12]
        cls.prefix = f"zovereign-it-{cls.run_id}"
        cls.images = {version: f"{cls.prefix}:{version}" for version in ("v1", "incompatible")}
        cls.addClassCleanup(cls.cleanup_resources)
        print(f"\nReal Docker context: {cls.context}; fixture prefix: {cls.prefix}", file=sys.stderr)
        for version, image in cls.images.items():
            print(f"Building {image}", file=sys.stderr)
            cls.docker("build", "--tag", image,
                       "--label", f"{RUN_LABEL}={cls.run_id}",
                       "--build-arg", f"FIXTURE_REVISION={version}",
                       "--build-arg", f"FIXTURE_DB_MAJOR={31 if version == 'incompatible' else 30}",
                       str(ROOT / "tests/docker_fixture"), timeout=600)

    @classmethod
    def execute(cls, command, *, check=True, timeout=180):
        result = subprocess.run(command, env=cls.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise AssertionError(f"Command failed ({result.returncode}): {' '.join(command)}\n"
                                 f"{result.stdout}\n{result.stderr}")
        return result

    @classmethod
    def docker(cls, *args, **kwargs):
        return cls.execute([*cls.docker_command, *args], **kwargs)

    @classmethod
    def cleanup_resources(cls):
        # Only this invocation's labelled objects are eligible. No prune or -v.
        failures = []
        for kind, list_args, inspect_args, remove_args in (
            ("container", ["ps", "--all", "--quiet"], ["container", "inspect"], ["rm", "--force"]),
            ("volume", ["volume", "ls", "--quiet"], ["volume", "inspect"], ["volume", "rm"]),
            ("image", ["image", "ls", "--quiet"], ["image", "inspect"], ["image", "rm"]),
        ):
            found = cls.docker(*list_args, "--filter", f"label={RUN_LABEL}={cls.run_id}", check=False)
            if found.returncode:
                failures.append(found.stderr)
                continue
            for resource in sorted(set(found.stdout.split())):
                observed = cls.docker(*inspect_args, resource, check=False)
                if observed.returncode:
                    failures.append(observed.stderr)
                    continue
                metadata = json.loads(observed.stdout)[0]
                labels = metadata.get("Config", {}).get("Labels") if kind != "volume" else metadata.get("Labels")
                if not labels or labels.get(RUN_LABEL) != cls.run_id:
                    failures.append(f"Refused cleanup of unowned {kind} {resource}")
                    continue
                removed = cls.docker(*remove_args, resource, check=False)
                if removed.returncode:
                    failures.append(removed.stderr)
        if failures:
            raise AssertionError(f"Cleanup incomplete for {cls.prefix}:\n" + "\n".join(failures))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zovereign-cli-it-")
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name) / "config"
        suffix = uuid.uuid4().hex[:8]
        self.name = f"{self.prefix}-{suffix}"
        self.volume = f"{self.name}-data"
        settings = f"{self.name}-settings"
        for volume in (self.volume, settings):
            self.docker("volume", "create", "--label", f"{RUN_LABEL}={self.run_id}", volume)
        # Explicit ephemeral-range host ports make preservation observable on
        # local and remote engines. Retry a collision without touching its owner.
        for attempt in range(3):
            self.port = str(random.SystemRandom().randrange(40000, 59000))
            result = self.docker("run", "--detach", "--name", self.name,
                                 "--label", f"{RUN_LABEL}={self.run_id}",
                                 "--restart", "unless-stopped", "--stop-timeout", "5",
                                 "--log-driver", "json-file", "--log-opt", "max-size=1m",
                                 "--log-opt", "max-file=2",
                                 "--mount", f"type=volume,src={self.volume},dst=/data",
                                 "--mount", f"type=volume,src={settings},dst=/fixture-settings,readonly",
                                 "--publish", f"127.0.0.1:{self.port}:9067",
                                 "--env", "DOWNLOAD_WORKERS=5", "--env", "FIXTURE_CUSTOM=preserve-me",
                                 self.images["v1"], check=False)
            if result.returncode == 0:
                break
            self.docker("rm", "--force", self.name, check=False)
            if attempt == 2 or not any(word in result.stderr.lower() for word in ("port is already allocated", "address already in use")):
                self.fail(result.stdout + result.stderr)
        self.original = self.inspect()
        self.wait_ready()
        self.identity = self.report()["identity"]
        self.cli("adopt", "--container", self.name, "--yes")

    def inspect(self, name=None):
        return json.loads(self.docker("container", "inspect", name or self.name).stdout)[0]

    def cli(self, *args, ok=True, config_dir=None):
        result = self.execute([str(BINARY), "--context", self.context, "--config-dir",
                               str(config_dir or self.config_dir), "--json", *args], check=False)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def config(self):
        return json.loads((self.config_dir / "installation.json").read_text())

    def report(self):
        return json.loads(self.docker("exec", self.name, "python3", "/opt/zovereign/stack.py", "health").stdout)

    def wait_ready(self, name=None):
        name = name or self.name
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = self.inspect(name)["State"]
            if state.get("Health", {}).get("Status") == "healthy":
                return
            if not state["Running"]:
                self.fail(f"Fixture stopped: {state}; {self.docker('logs', name, check=False).stdout}")
            time.sleep(0.25)
        self.fail(f"Fixture readiness timeout: {self.inspect(name)['State']}")


    def assert_persistent_identity(self, revision):
        report = self.report()
        self.assertTrue(report["test_fixture"])
        self.assertEqual(report["identity"], self.identity)
        self.assertEqual(report["revision"], revision)
        data_mount = next(m for m in self.inspect()["Mounts"] if m["Destination"] == "/data")
        self.assertEqual(data_mount["Name"], self.volume)

    def test_adoption_setup_and_daily_observations(self):
        current = self.inspect()
        for key in ("Id", "Config", "HostConfig"):
            self.assertEqual(current[key], self.original[key], key)
        # Docker returns mounts from a map; their order can change between reads.
        self.assertEqual(sorted(current["Mounts"], key=lambda m: m["Destination"]),
                         sorted(self.original["Mounts"], key=lambda m: m["Destination"]))
        # The first-run setup path also adopts an existing container before
        # applying the full-mainnet capacity gate. No resource check is weakened.
        self.cli("setup", "--container", self.name, "--yes", config_dir=Path(self.tmp.name) / "auto-adopt")
        self.assertEqual(self.inspect()["Id"], self.original["Id"])
        self.assertTrue(json.loads(self.cli("health").stdout)["ready"])
        self.assertTrue(json.loads(self.cli("status").stdout)["runtime"]["test_fixture"])
        self.assertEqual(json.loads(self.cli("stats").stdout)["Name"], self.name)
        self.assertIn("fixture_ready", self.cli("logs").stderr)
        binding = json.loads(self.cli("connect").stdout)["bindings"][0]
        self.assertEqual(binding, {"HostIp": "127.0.0.1", "HostPort": self.port})

    def test_start_stop_preserves_data_and_intent(self):
        starts = self.report()["starts"]
        self.cli("stop")
        self.assertFalse(self.inspect()["State"]["Running"])
        self.assertFalse(self.config()["desired_running"])
        self.cli("health", ok=False)
        self.cli("start")
        self.wait_ready()
        self.assert_persistent_identity("v1")
        self.assertGreater(self.report()["starts"], starts)

    def test_unsupported_database_adoption_preserves_the_running_container(self):
        name = f"{self.name}-unsupported"
        volume = f"{name}-data"
        self.docker("volume", "create", "--label", f"{RUN_LABEL}={self.run_id}", volume)
        self.docker("run", "--detach", "--name", name,
                    "--label", f"{RUN_LABEL}={self.run_id}",
                    "--mount", f"type=volume,src={volume},dst=/data",
                    self.images["incompatible"])
        self.wait_ready(name)
        before = self.inspect(name)
        identity = self.docker("exec", name, "cat", "/data/identity.json").stdout
        config = Path(self.tmp.name) / "unsupported-config"
        result = self.cli("adopt", "--container", name, "--yes", config_dir=config, ok=False)
        self.assertIn("Unsupported runtime versions", json.loads(result.stdout)["error"])
        self.assertFalse((config / "installation.json").exists())
        after = self.inspect(name)
        self.assertEqual(after["Id"], before["Id"])
        self.assertTrue(after["State"]["Running"])
        self.assertEqual(self.docker("exec", name, "cat", "/data/identity.json").stdout, identity)









if __name__ == "__main__":
    unittest.main()
