"""Shared CLI test fixtures: monitor service environments and a local release server."""
import hashlib
import os
import plistlib
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = "https://github.com/zcashlabs/zovereign"
PLATFORMS = {
    ("Linux", "x86_64"): "linux-amd64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Darwin", "x86_64"): "darwin-amd64",
    ("Darwin", "arm64"): "darwin-arm64",
}


def monitor_environment(template, platform):
    if platform == "macos":
        return plistlib.loads(template.encode()).get("EnvironmentVariables", {})
    if platform == "linux":
        environment = {}
        for line in template.splitlines():
            if line.startswith("Environment="):
                for assignment in shlex.split(line[len("Environment="):]):
                    name, value = assignment.split("=", 1)
                    environment[name] = value.replace("%%", "%")
        return environment
    raise ValueError(f"Unsupported monitor platform: {platform}")


class ReleaseFixture(unittest.TestCase):
    """Serve signed-looking release assets through fake curl and Cosign on PATH."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tools = self.root / "tools"
        self.assets = self.root / "assets"
        self.home = self.root / "home with spaces"
        for directory in (self.tools, self.assets, self.home):
            directory.mkdir()
        for name in ("awk", "chmod", "cp", "grep", "mkdir", "mktemp", "mv", "rm",
                     "shasum", "sha256sum"):
            if path := shutil.which(name):
                (self.tools / name).symlink_to(path)
        self.bin = self.home / ".local/bin/zovereign"
        self.profile = self.home / ".bashrc"
        self.tag = "v1.2.3"
        self.env = {
            **os.environ, "PATH": str(self.tools), "HOME": str(self.home),
            "SHELL": "/bin/bash", "TMPDIR": str(self.root), "OS_NAME": "Linux",
            "ARCH": "x86_64", "TRANSLATED": "0", "REPO": REPO, "TAG": self.tag,
            "ASSETS": str(self.assets),
        }
        self.tool("uname", '#!/bin/sh\ncase "$1" in -s) echo "$OS_NAME";; -m) echo "$ARCH";; esac\n')
        self.tool("sysctl", '#!/bin/sh\necho "$TRANSLATED"\n')
        self.tool("curl", f"#!{sys.executable}\n" + """
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
repo, tag, url = os.environ['REPO'], os.environ['TAG'], args[-1]
if url == repo + '/releases/latest':
    # The CLI also asks for the status code; the installer asks only for the URL.
    status = '200 ' if '%{http_code}' in ' '.join(args) else ''
    print(status + repo + '/releases/tag/' + tag, end='')
else:
    base, name = url.rsplit('/', 1)
    assert base in (repo + '/releases/download/' + tag,
                    'https://github.com/sigstore/cosign/releases/download/v3.1.4')
    src = Path(os.environ['ASSETS'], name)
    if not src.is_file():
        sys.exit(22)
    shutil.copyfile(src, args[args.index('--output') + 1])
""")
        # Isolate the network signature boundary. The release workflow also runs this
        # installer against the real signed assets and the production Cosign binary.
        self.tool("cosign", f"#!{sys.executable}\n" + """
import hashlib, os, sys
from pathlib import Path
args = sys.argv[1:]
identity = os.environ['REPO'] + '/.github/workflows/release.yml@refs/heads/main'
assert args[:2] == ['verify-blob', '--bundle']
assert args[3:-1] == ['--certificate-identity', identity, '--certificate-oidc-issuer',
                      'https://token.actions.githubusercontent.com']
assert Path(args[2]).read_text() == hashlib.sha256(Path(args[-1]).read_bytes()).hexdigest()
""")
        self.manifest = self.assets / "SHA256SUMS"
        self.bundle = self.assets / "SHA256SUMS.sigstore.json"
        self.release(self.tag, lambda platform: f"#!/bin/sh\necho {platform}\n".encode())

    def release(self, tag, contents):
        """Publish `contents` as the binary for every platform under `tag`."""
        self.tag = self.env["TAG"] = tag
        sums = []
        for platform in PLATFORMS.values():
            name = f"zovereign-{tag}-{platform}"
            data = contents(platform)
            (self.assets / name).write_bytes(data)
            (self.assets / name).chmod(0o755)
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        self.manifest.write_text("".join(sums))
        self.sign_fixture()

    def tool(self, name, script):
        path = self.tools / name
        path.write_text(script)
        path.chmod(0o755)

    def sign_fixture(self):
        self.bundle.write_text(hashlib.sha256(self.manifest.read_bytes()).hexdigest())
