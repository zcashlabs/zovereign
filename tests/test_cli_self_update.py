"""Update a release build of the CLI through its embedded installer and a local release."""

import copy
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import test_cli_workflows as workflows
from cli_test_support import ReleaseFixture

ROOT = Path(__file__).resolve().parents[1]
# Records its arguments and reports one JSON result, like a newer CLI's runtime update.
NEW_RELEASE = """#!/bin/sh
printf '%s\\n' "$@" > "$HANDOFF_ARGS"
echo '{"available":false,"image":"handed-off"}'
"""


@unittest.skipUnless(os.environ.get("ZOVEREIGN_TEST_BINARY"), "requires a release build of the CLI")
class SelfUpdate(ReleaseFixture):
    def setUp(self):
        super().setUp()
        binary = Path(os.environ["ZOVEREIGN_TEST_BINARY"]).resolve(strict=True)
        version = subprocess.check_output([binary, "--version"], text=True).split()[1]
        major, minor, patch = (int(part) for part in version.split("."))
        self.current = f"v{version}"
        self.newer = f"v{major}.{minor}.{patch + 1}"
        # Self-update replaces the running file, so never run the build output itself.
        self.cli_path = self.root / "install dir/zovereign"
        self.cli_path.parent.mkdir()
        shutil.copy2(binary, self.cli_path)
        self.original = self.cli_path.read_bytes()
        self.release(self.newer, lambda platform: NEW_RELEASE.encode())
        (self.tools / "python3").symlink_to(sys.executable)
        self.docker_state = self.root / "docker.json"
        self.docker_state.write_text(json.dumps({
            "images": {"sha256:old": workflows.IMAGE, "candidate": copy.deepcopy(workflows.IMAGE),
                       "ghcr.io/zcashlabs/zovereign:latest": copy.deepcopy(workflows.IMAGE)},
            "containers": {"original": workflows.CONTAINER}, "template": workflows.CONTAINER}))
        self.env.update(ZOVEREIGN_DOCKER=str(ROOT / "tests/fake_docker.py"),
                        FAKE_DOCKER_STATE=str(self.docker_state),
                        HANDOFF_ARGS=str(self.root / "handoff-args"))
        self.config = self.root / "config"

    def cli(self, *args, success=True):
        result = subprocess.run([str(self.cli_path), "--config-dir", str(self.config), "--json", *args],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def assert_unchanged(self):
        self.assertEqual(self.cli_path.read_bytes(), self.original)

    def test_search_reports_a_newer_cli_without_installing_it(self):
        result = self.cli("update", "--search")
        self.assertEqual(result["cli_latest"], self.newer)
        self.assertEqual(result["cli_update"], "available")
        self.assertIn("not set up", result["installation"])
        self.assert_unchanged()

    def test_current_release_is_up_to_date(self):
        self.release(self.current, lambda platform: NEW_RELEASE.encode())
        self.assertEqual(self.cli("update", "--search")["cli_update"], "up to date")
        self.assertEqual(self.cli("update", "--yes")["cli_update"], "up to date")
        self.assert_unchanged()

    def test_cli_only_update_replaces_the_running_binary_in_place(self):
        result = self.cli("update", "--cli-only", "--yes")
        self.assertEqual(result["cli_update"], f"updated to {self.newer}")
        self.assertEqual(self.cli_path.read_bytes(), NEW_RELEASE.encode())
        self.assertEqual(sorted(self.cli_path.parent.iterdir()), [self.cli_path])
        self.assertFalse((self.home / ".bashrc").exists(), "self-update must not edit the shell PATH")

    def test_stack_update_hands_the_runtime_update_to_the_new_cli(self):
        self.cli("adopt", "--yes")
        result = self.cli("update", "--yes")
        self.assertEqual(result, {"available": False, "image": "handed-off",
                                  "cli_update": f"updated to {self.newer}"})
        self.assertEqual(Path(self.env["HANDOFF_ARGS"]).read_text().splitlines(),
                         ["--config-dir", str(self.config), "--json", "--yes", "update", "--runtime-only"])

    def test_rejected_release_keeps_the_current_cli(self):
        self.bundle.write_text("forged signature")
        self.assertIn("unchanged", self.cli("update", "--cli-only", "--yes", success=False)["error"])
        self.assert_unchanged()

    def test_explicit_runtime_updates_never_contact_releases(self):
        self.cli("adopt", "--yes")
        self.tool("curl", "#!/bin/sh\necho 'unexpected release lookup' >&2\nexit 1\n")
        for args in (("update", "--check", "--image", "candidate"),
                     ("update", "--runtime-only", "--check"),
                     ("update", "--search", "--local-image", "--image", "candidate")):
            with self.subTest(args=args):
                self.assertNotIn("cli_update", self.cli(*args))
        self.assert_unchanged()

    def test_confirmation_is_required_before_replacing_the_cli(self):
        result = subprocess.run([str(self.cli_path), "--config-dir", str(self.config), "update", "--cli-only"],
                                env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--yes", result.stderr)
        self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
