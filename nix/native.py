#!/usr/bin/env python3
"""Native Nix entrypoint; the Docker entrypoint retains its existing defaults."""

import argparse
import os
from pathlib import Path
import sys

import stack


def configure_environment():
    if os.getuid() == 0:
        raise stack.StackError("Run the native stack as your ordinary user, without sudo")
    if "DATA_DIR" not in os.environ:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        os.environ["DATA_DIR"] = str(base / "zovereign")
    if not os.environ["DATA_DIR"]:
        raise stack.StackError("DATA_DIR must not be empty")
    os.environ["DATA_DIR"] = str(Path(os.environ["DATA_DIR"]).expanduser().resolve())
    os.environ.setdefault("GRPC_BIND_ADDR", "127.0.0.1:9067")
    # Compatibility metadata describes the packaged binaries, not user config.
    os.environ.update(ZAKURA_VERSION="1.8.7", ZAKURA_DB_MAJOR="30", LIGHTWALLETD_VERSION="v0.6.0")


def main():
    parser = argparse.ArgumentParser(
        description="Restore mainnet snapshots and run Zakura + lightwalletd in the foreground.",
        epilog="Ctrl-C stops both services and preserves data. Override storage with DATA_DIR; "
               "gRPC defaults to 127.0.0.1:9067 (GRPC_BIND_ADDR). No .env file is read.",
    )
    parser.add_argument("command", choices=("run", "health"), nargs="?", default="run",
                        help="run the stack (default), or check RPC and a real compact block")
    args = parser.parse_args()
    try:
        configure_environment()
    except stack.StackError as error:
        print(f"[zovereign] ERROR: {error}", file=sys.stderr)
        return 1
    sys.argv = [str(Path(stack.__file__)), args.command]
    return stack.main()


if __name__ == "__main__":
    sys.exit(main())
