"""Exercise the shipped installer in an isolated home with local release assets."""

import hashlib
import os
import platform
import shutil
import subprocess
import unittest
from pathlib import Path

from cli_test_support import PLATFORMS, ReleaseFixture


INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install-cli.sh"


class Installer(ReleaseFixture):
    def install(self, *args, success=True):
        result = subprocess.run(["/bin/sh", str(INSTALLER), *args], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_platforms_and_rosetta(self):
        for (os_name, arch), platform in PLATFORMS.items():
            with self.subTest(os=os_name, arch=arch):
                self.env.update(OS_NAME=os_name, ARCH=arch)
                self.install()
                self.assertEqual(subprocess.check_output([self.bin], text=True).strip(), platform)
        self.env.update(OS_NAME="Darwin", ARCH="x86_64", TRANSLATED="1")
        self.install(self.tag)
        self.assertEqual(subprocess.check_output([self.bin], text=True).strip(), "darwin-arm64")

    def test_fresh_shell_and_reinstall(self):
        profile = self.home / ".bash_profile"
        profile.write_text("# existing login configuration\n")
        self.install()
        content = profile.read_text()
        self.install(self.tag)
        self.assertEqual(profile.read_text(), content)
        self.assertIn("# existing login configuration", content)
        for startup in (self.profile, profile):
            result = subprocess.check_output(
                ["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; command -v zovereign',
                 "bash", str(startup)], env=self.env, text=True)
            self.assertEqual(result.strip(), str(self.bin))

    @unittest.skipUnless(os.environ.get("ZOVEREIGN_TEST_BINARY"), "requires a native CLI build")
    def test_install_working_tree_binary(self):
        binary = Path(os.environ["ZOVEREIGN_TEST_BINARY"]).resolve(strict=True)
        version = subprocess.check_output([binary, "--version"], text=True).strip()
        self.assertRegex(version, r"^zovereign [0-9]+\.[0-9]+\.[0-9]+$")
        tag = "v" + version.split()[1]
        system, machine = platform.system(), platform.machine()
        if system == "Linux" and machine == "arm64":
            machine = "aarch64"
        self.env.update(OS_NAME=system, ARCH=machine, TAG=tag)
        asset = self.assets / f"zovereign-{tag}-{PLATFORMS[(system, machine)]}"
        shutil.copyfile(binary, asset)
        self.manifest.write_text(f"{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}\n")
        self.sign_fixture()
        self.install()
        output = subprocess.check_output(
            ["/bin/bash", "--noprofile", "--norc", "-ec",
             '. "$HOME/.bashrc"; command -v zovereign; zovereign --version; zovereign --help >/dev/null'],
            env=self.env, text=True)
        self.assertEqual(output.splitlines(), [str(self.bin), version])

    def test_failure_preserves_previous_installation(self):
        self.install()
        self.bin.write_text("#!/bin/sh\necho previous\n")
        original_profile = self.profile.read_text()
        candidate = self.assets / f"zovereign-{self.tag}-linux-amd64"
        for file, data in ((self.bundle, b"invalid signature"), (self.manifest, b"forged"),
                           (candidate, b"corrupt"), (candidate, None)):
            with self.subTest(file=file.name, data=data):
                old = file.read_bytes()
                if data is None:
                    file.unlink()
                else:
                    file.write_bytes(data)
                self.install(success=False)
                self.assertEqual(subprocess.check_output([self.bin], text=True).strip(), "previous")
                self.assertEqual(self.profile.read_text(), original_profile)
                self.assertEqual(list(self.bin.parent.iterdir()), [self.bin])
                file.write_bytes(old)

    @unittest.skipUnless(shutil.which("zsh"), "requires Zsh")
    def test_new_zsh_session_finds_installed_binary(self):
        self.tool("zovereign", "#!/bin/sh\necho old\n")
        self.env.update(SHELL=shutil.which("zsh"), ZDOTDIR=str(self.home / "zsh config"))
        self.env["PATH"] += os.pathsep + str(self.bin.parent)
        self.install()
        self.install(self.tag)
        output = subprocess.check_output(
            [self.env["SHELL"], "-d", "-i", "-c", "command -v zovereign"],
            env=self.env, text=True)
        self.assertEqual(output.strip(), str(self.bin))

    def test_invalid_platform_tag_checksum_or_verifier(self):
        self.env["ARCH"] = "i686"
        self.install(success=False)
        self.env["ARCH"] = "x86_64"
        self.install("../../bad", success=False)
        original = self.manifest.read_text()
        for manifest in ("", original + original):
            self.manifest.write_text(manifest)
            self.sign_fixture()
            self.install(success=False)
        self.manifest.write_text(original)
        self.sign_fixture()
        (self.tools / "cosign").unlink()
        (self.assets / "cosign-linux-amd64").write_bytes(b"bogus verifier")
        self.assertIn("SHA-256 mismatch", self.install(success=False).stderr)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.profile.exists())

    @unittest.skipUnless(os.environ.get("ZOVEREIGN_SIGNED_DIST"), "release workflow only")
    def test_signed_release(self):
        # Only HTTP transport is local. Exercise the production signer identity,
        # verifier, signed checksums and the actual release executable before publish.
        self.env.update(ASSETS=os.environ["ZOVEREIGN_SIGNED_DIST"], TAG=os.environ["RELEASE_TAG"])
        for tool in ("uname", "cosign"):
            path = shutil.which(tool)
            self.assertIsNotNone(path)
            (self.tools / tool).unlink()
            (self.tools / tool).symlink_to(path)
        self.env["PATH"] += os.pathsep + os.environ["PATH"]
        self.install(self.env["TAG"])
        output = subprocess.check_output([self.bin, "--version"], text=True)
        self.assertEqual(output.strip(), "zovereign " + self.env["TAG"][1:])


if __name__ == "__main__":
    unittest.main()
