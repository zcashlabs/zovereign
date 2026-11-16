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
from cli_test_support import monitor_environment

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
        cls.images = {version: f"{cls.prefix}:{version}" for version in ("v1", "v2", "incompatible")}
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

    def update(self):
        self.cli("update", "--local-image", "--image", self.images["v2"], "--yes")
        self.wait_ready()

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
        self.assertTrue(json.loads(self.cli("doctor").stdout)["data_preserved"])

    def test_start_stop_and_monitor_preserve_data_and_intent(self):
        starts = self.report()["starts"]
        self.cli("stop")
        self.assertFalse(self.inspect()["State"]["Running"])
        self.assertFalse(self.config()["desired_running"])
        self.cli("agent", "--once", "--no-update-check")
        self.cli("repair", "--yes", ok=False)
        self.assertFalse(self.inspect()["State"]["Running"])
        self.cli("health", ok=False)
        self.cli("start")
        self.wait_ready()
        self.assert_persistent_identity("v1")
        self.assertGreater(self.report()["starts"], starts)

    def test_monitor_can_find_real_docker_on_both_platforms(self):
        for platform in ("linux", "macos"):
            with self.subTest(platform=platform):
                rendered = json.loads(self.cli("startup", "render", "--platform", platform).stdout)
                # Service environments must retain Docker configuration even
                # when HOME/PATH differ from the installing terminal.
                environment = {**self.env, "HOME": str(Path(self.tmp.name) / "service-home"),
                               "PATH": "", "DOCKER_CONFIG": str(Path(self.tmp.name) / "wrong-config"),
                               **monitor_environment(rendered["template"], platform)}
                result = subprocess.run([str(BINARY), "--config-dir", str(self.config_dir),
                                         "agent", "--once", "--no-update-check"],
                                        env=environment, text=True, capture_output=True, timeout=180)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((self.config_dir / "monitor.json").read_text())
                self.assertNotIn("error", report["status"], report)
                self.assertTrue(report["status"]["runtime"]["test_fixture"])
                self.assertEqual(self.inspect()["Id"], self.original["Id"])

    def test_compatible_replacement_preserves_volume_and_settings(self):
        candidate = json.loads(self.cli("update", "--check", "--local-image", "--image", self.images["v2"]).stdout)
        self.assertTrue(candidate["available"])
        self.assertTrue(candidate["compatible"])
        self.assertEqual(self.inspect()["Id"], self.original["Id"])
        self.update()
        current = self.inspect()
        self.assertNotEqual(current["Id"], self.original["Id"])
        self.assertNotEqual(current["Image"], self.original["Image"])
        self.assert_persistent_identity("v2")
        for key in ("RestartPolicy", "PortBindings", "LogConfig"):
            self.assertEqual(current["HostConfig"][key], self.original["HostConfig"][key], key)
        self.assertEqual(sorted((m["Name"], m["Destination"], m["RW"]) for m in current["Mounts"]),
                         sorted((m["Name"], m["Destination"], m["RW"]) for m in self.original["Mounts"]))
        self.assertIn("DOWNLOAD_WORKERS=5", current["Config"]["Env"])
        self.assertIn("FIXTURE_CUSTOM=preserve-me", current["Config"]["Env"])
        self.assertEqual(current["Config"]["Labels"]["org.opencontainers.image.revision"], "v2")
        retained = self.inspect(self.config()["previous"]["container_id"])
        self.assertFalse(retained["State"]["Running"])
        self.assertEqual(retained["HostConfig"]["RestartPolicy"]["Name"], "no")

    def test_rollback_restores_original_container_and_data(self):
        self.update()
        replacement = self.inspect()["Id"]
        self.docker("stop", "--time", "5", self.name)
        self.cli("update", "--accept", "--yes", ok=False)
        self.assertIsNotNone(self.config()["previous"])
        self.cli("update", "--rollback", "--yes")
        self.wait_ready()
        self.assertEqual(self.inspect()["Id"], self.original["Id"])
        self.assert_persistent_identity("v1")
        self.assertFalse(self.inspect(replacement)["State"]["Running"])
        self.assertEqual(self.inspect(replacement)["HostConfig"]["RestartPolicy"]["Name"], "no")
        self.assertIsNone(self.config()["previous"])

    def test_accept_removes_only_predecessor_and_keeps_volume(self):
        # A second live service must stay untouched by management of the target.
        guard = f"{self.name}-unmanaged"
        guard_volume = f"{guard}-data"
        self.docker("volume", "create", "--label", f"{RUN_LABEL}={self.run_id}", guard_volume)
        self.docker("run", "--detach", "--name", guard, "--label", f"{RUN_LABEL}={self.run_id}",
                    "--mount", f"type=volume,src={guard_volume},dst=/data", self.images["v1"])
        self.wait_ready(guard)
        guard_id = self.inspect(guard)["Id"]
        guard_identity = self.docker("exec", guard, "cat", "/data/identity.json").stdout
        self.update()
        self.cli("update", "--accept", "--yes")
        self.assertNotEqual(self.docker("container", "inspect", self.original["Id"], check=False).returncode, 0)
        self.assert_persistent_identity("v2")
        self.docker("volume", "inspect", self.volume)
        self.assertIsNone(self.config()["previous"])
        self.assertEqual(self.inspect(guard)["Id"], guard_id)
        self.assertTrue(self.inspect(guard)["State"]["Running"])
        self.assertEqual(self.docker("exec", guard, "cat", "/data/identity.json").stdout, guard_identity)

    def test_incompatible_and_missing_images_never_stop_runtime(self):
        before = self.inspect()
        for image in (self.images["incompatible"], f"{self.prefix}:missing"):
            self.cli("update", "--local-image", "--image", image, "--yes", ok=False)
            after = self.inspect()
            self.assertEqual(after["Id"], before["Id"])
            self.assertTrue(after["State"]["Running"])
            self.assertEqual(after["State"]["StartedAt"], before["State"]["StartedAt"])
            self.assertIsNone(self.config()["previous"])
        self.assert_persistent_identity("v1")

    def test_update_of_stopped_installation_stays_stopped(self):
        self.cli("stop")
        self.cli("update", "--local-image", "--image", self.images["v2"], "--yes")
        self.assertFalse(self.inspect()["State"]["Running"])
        self.cli("agent", "--once", "--no-update-check")
        self.assertFalse(self.inspect()["State"]["Running"])
        self.cli("start")
        self.wait_ready()
        self.assert_persistent_identity("v2")

    def test_repair_recovers_retained_container_from_seeded_journal(self):
        # Seed the durable old_retained stage with real Docker transitions.
        # This checks recovery, not the timing of an actual host/process crash.
        config = self.config()
        self.docker("stop", "--time", "5", self.name)
        self.docker("update", "--restart", "no", self.name)
        self.docker("rename", self.name, f"{self.name}-retained")
        config["previous"] = {"container_id": self.original["Id"], "image_id": self.original["Image"],
                              "compatibility": {"zakura": "1.8.7", "database_major": "30", "lightwalletd": "v0.6.0"},
                              "restart_policy": "unless-stopped"}
        target = json.loads(self.docker("image", "inspect", self.images["v2"]).stdout)[0]["Id"]
        config["pending"] = {"stage": "old_retained", "old_id": self.original["Id"],
                             "new_image": target, "was_running": True}
        (self.config_dir / "installation.json").write_text(json.dumps(config))
        self.assertTrue(json.loads(self.cli("doctor").stdout)["pending"])
        self.cli("repair", "--action", "rollback", "--yes")
        self.wait_ready()
        self.assertEqual(self.inspect()["Id"], self.original["Id"])
        self.assert_persistent_identity("v1")
        self.assertIsNone(self.config()["pending"])


if __name__ == "__main__":
    unittest.main()
