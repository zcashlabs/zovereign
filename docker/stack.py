#!/usr/bin/env python3
"""Bootstrap and supervise a self-contained mainnet Zakura + lightwalletd stack."""

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ZAKURA_MANIFEST = "https://zakura.valargroup.dev/mainnet/snapshots.json"
LIGHTWALLETD_MANIFEST = "https://165-232-183-161.sslip.io/latest.json"
RPC_URL = "http://127.0.0.1:8232"
CHILDREN = set()
VOLUME_LOCK = None
LOG_THREADS = []
STATUS_ROOT = None
PROGRESS = {}


class StackError(Exception):
    pass


class Shutdown(BaseException):
    pass


def log(message):
    print(f"[zovereign] {message}", flush=True)


def positive_int(value, name, minimum=1):
    if not str(value).isdigit() or int(value) < minimum:
        raise StackError(f"{name} must be an integer >= {minimum}")
    return int(value)


def spawn(args, **kwargs):
    process = subprocess.Popen(args, start_new_session=True, **kwargs)
    CHILDREN.add(process)
    return process


def stop(process, grace=90):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as signal_error:
            # A short-lived child can disappear after poll but before killpg.
            # Only ignore a signal error when wait proves the child has exited.
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                raise signal_error
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    CHILDREN.discard(process)


def command(args, capture=False, timeout=None, **kwargs):
    process = spawn(args, stdout=subprocess.PIPE if capture else None, **kwargs)
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode:
            raise StackError(f"{Path(args[0]).name} exited with status {process.returncode}")
        return output
    finally:
        stop(process, grace=5)


def shutdown(_number, _frame):
    raise Shutdown()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def progress(phase, service=None, **fields):
    """Publish durable progress for CLI/desktop readers; probes never write it."""
    if STATUS_ROOT is None:
        return
    PROGRESS.clear()
    PROGRESS.update(schema_version=1, phase=phase, service=service,
                    updated_at=int(time.time()), **fields)
    try:
        write_json(STATUS_ROOT / "status.json", PROGRESS)
    except OSError as error:
        log(f"Could not publish progress: {error}")


def status(root):
    """Observe an existing installation without starting bootstrap or daemons."""
    path = root / "status.json"
    result = json.loads(path.read_text()) if path.is_file() else {
        "schema_version": 1, "phase": "unknown", "message": "No progress recorded yet"
    }
    disk = shutil.disk_usage(root)
    result["storage"] = {"free_bytes": disk.free, "total_bytes": disk.total}
    try:
        node = blockchain_info()
        result["node"] = {key: node.get(key) for key in ("blocks", "estimatedheight", "bestblockhash", "pruned", "chain")}
    except (OSError, ValueError, StackError) as error:
        result["node_error"] = str(error)
    return result


def http_url(value):
    if not isinstance(value, str) or urlparse(value).scheme not in ("https", "http"):
        raise StackError("Snapshot URLs must use HTTP or HTTPS")
    return value


def fetch_json(url):
    # curl also works with publishers that reject Python's default HTTP client.
    return json.loads(command([
        "curl", "--fail", "--location", "--silent", "--show-error",
        "--proto", "=http,https", "--proto-redir", "=http,https",
        "--connect-timeout", "20", "--max-time", "120", "--retry", "3", url,
    ], capture=True))


def validate_snapshot(kind, item):
    if not isinstance(item, dict) or item.get("network") != "mainnet":
        raise StackError(f"{kind} snapshot must be for mainnet")
    if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item["sha256"]):
        raise StackError(f"{kind} snapshot must have a SHA-256 checksum")
    http_url(item.get("url"))
    positive_int(item.get("size"), f"{kind} snapshot size")
    if kind == "zakura":
        if item.get("snapshot_kind") != "archive" or item.get("db_major") != int(os.environ.get("ZAKURA_DB_MAJOR", "30")):
            raise StackError("Zakura snapshot must be an archive with the image's supported database major")
    elif item.get("lightwalletd_version") != os.environ.get("LIGHTWALLETD_VERSION", "v0.6.0"):
        raise StackError("lightwalletd snapshot version does not match the bundled binary")


def choose_snapshot(kind, manifest_url):
    manifest = fetch_json(http_url(manifest_url))
    if kind == "zakura":
        if not isinstance(manifest, list):
            raise StackError("Zakura manifest must contain a snapshot list")
        item = next((item.copy() for item in manifest if item.get("network") == "mainnet"
                     and item.get("snapshot_kind") == "archive" and "latest" in item.get("roles", [])), None)
        if item is None:
            raise StackError("No latest mainnet archive in the Zakura manifest")
        item["size"] = item.get("size_bytes")
    else:
        if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
            raise StackError("Unsupported lightwalletd manifest format")
        item = manifest.copy()
        item["url"] = item.get("snapshot_url")
    validate_snapshot(kind, item)
    item["manifest_url"] = manifest_url
    return item


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as archive:
        while chunk := archive.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download(item, archive, chunk_bytes, reserve_bytes, workers=1):
    """Append verified HTTP ranges; retain complete ranges across restarts."""
    size = int(item["size"])
    offset = archive.stat().st_size if archive.exists() else 0
    if offset > size:
        raise StackError(f"Cached archive is larger than its manifest: {archive}")
    if shutil.disk_usage(archive.parent).free < size - offset + reserve_bytes:
        raise StackError("Insufficient free disk for the archive and reserve; use a larger data volume")
    workers = positive_int(workers, "DOWNLOAD_WORKERS")
    if workers > 8:
        raise StackError("DOWNLOAD_WORKERS must not exceed 8")
    for stale in archive.parent.glob(archive.name + ".range-*"):
        stale.unlink()
    pending = []
    jobs = []
    next_offset = offset

    def enqueue():
        nonlocal next_offset
        if next_offset >= size:
            return
        end = min(next_offset + chunk_bytes, size) - 1
        chunk = archive.with_name(f"{archive.name}.range-{next_offset}")
        headers = chunk.with_name(chunk.name + ".headers")
        args = [
            "curl", "--fail", "--location", "--silent", "--show-error", "--http1.1",
            "--proto", "=http,https", "--proto-redir", "=http,https",
            "--connect-timeout", "20", "--speed-limit", "1024", "--speed-time", "60",
            "--max-filesize", str(end - next_offset + 1),
            "--range", f"{next_offset}-{end}", "--dump-header", str(headers),
            "--output", str(chunk), item["url"],
        ]
        job = {"start": next_offset, "end": end, "chunk": chunk, "headers": headers, "args": args}
        jobs.append(job)
        job["process"] = spawn(args)
        pending.append(job)
        next_offset = end + 1

    progress("downloading", archive.name.split(".")[0], completed_bytes=offset, total_bytes=size)
    log(f"Downloading {archive.name}: {offset:,}/{size:,} bytes (resuming stored progress; {workers} connections)")
    try:
        for _ in range(workers):
            enqueue()
        with archive.open("ab") as output:
            while pending:
                job = pending.pop(0)
                end, chunk, headers = job["end"], job["chunk"], job["headers"]
                expected = end - offset + 1
                for attempt in range(1, 6):
                    try:
                        process = job["process"]
                        result = process.wait()
                        CHILDREN.discard(process)
                        if result != 0:
                            raise StackError(f"curl exited with status {result}")
                        response_headers = headers.read_text().lower()
                        ranges = re.findall(r"^content-range:\s*bytes (\d+)-(\d+)/(\d+)\s*$", response_headers, re.M)
                        full_response = offset == 0 and end + 1 == size and not ranges
                        if (not full_response and (not ranges or tuple(map(int, ranges[-1])) != (offset, end, size))) or chunk.stat().st_size != expected:
                            raise StackError("Publisher returned an incorrect HTTP byte range or size")
                        break
                    except StackError:
                        if attempt == 5:
                            raise
                        log(f"Retrying range {offset}-{end}, attempt {attempt + 1}/5")
                        time.sleep(min(attempt * 2, 10))
                        job["process"] = spawn(job["args"])
                try:
                    with chunk.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                except BaseException:
                    output.truncate(offset)
                    raise
                offset = end + 1
                progress("downloading", archive.name.split(".")[0], completed_bytes=offset, total_bytes=size)
                log(f"{archive.name}: {offset:,}/{size:,} bytes ({offset * 100 // size}%)")
                chunk.unlink(missing_ok=True)
                headers.unlink(missing_ok=True)
                jobs.remove(job)
                enqueue()
    finally:
        for job in jobs:
            if "process" in job:
                stop(job["process"], grace=5)
            job["chunk"].unlink(missing_ok=True)
            job["headers"].unlink(missing_ok=True)
    progress("verifying", archive.name.split(".")[0], total_bytes=size)
    log(f"Verifying SHA-256 of {archive.name}")
    if sha256(archive) != item["sha256"].lower():
        archive.unlink()
        raise StackError("Snapshot checksum mismatch; discarded the corrupt archive")


def valid_database(kind, path):
    if kind == "zakura":
        database = path / "state" / f"v{os.environ.get('ZAKURA_DB_MAJOR', '30')}" / "mainnet"
        return (database / "CURRENT").is_file() and any(database.glob("MANIFEST-*"))
    return all((path / "db/main" / name).is_file() and (path / "db/main" / name).stat().st_size > 0
               for name in ("blocks", "lengths"))


def extract(kind, archive, destination, reserve_bytes):
    """Accept regular database files/directories only, inside a staging tree."""
    allowed = "state" if kind == "zakura" else "db"
    process = spawn(["zstd", "--decompress", "--stdout", str(archive)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as contents:
            for member in contents:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or (name.parts and name.parts[0] != allowed):
                    raise StackError(f"Unexpected archive path: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise StackError(f"Links and special files are not allowed: {member.name}")
                if member.isfile() and shutil.disk_usage(destination).free < member.size + reserve_bytes:
                    raise StackError("Insufficient free disk to extract snapshot; downloaded archive retained")
                contents.extract(member, path=destination, filter="data")
        # Drain any tar padding so zstd can validate the entire compressed frame.
        while process.stdout.read(1024 * 1024):
            pass
        if process.wait() != 0:
            raise StackError("Zstandard archive decompression failed")
    finally:
        process.stdout.close()
        stop(process, grace=5)
    if not valid_database(kind, destination):
        raise StackError(f"Archive does not contain the expected {kind} mainnet database")


def bootstrap(kind, root, manifest_url, chunk_bytes, reserve_bytes):
    progress("checking_database", kind)
    target = root / kind
    marker = target / ".bootstrap.json"
    cache = root / "bootstrap"
    archive = cache / f"{kind}.tar.zst.part"
    descriptor = cache / f"{kind}.json"
    staging = cache / f"{kind}.restore"
    if marker.is_file():
        validate_snapshot(kind, json.loads(marker.read_text()))
        if not valid_database(kind, target):
            raise StackError(f"Previously bootstrapped {kind} database is incomplete; refusing to replace it")
        archive.unlink(missing_ok=True)
        descriptor.unlink(missing_ok=True)
        log(f"Reusing existing {kind} database; no snapshot download")
        return
    if target.exists():
        if any(target.iterdir()):
            raise StackError(f"{target} contains unrecognized data; refusing to overwrite it")
        target.rmdir()
    if descriptor.exists():
        item = json.loads(descriptor.read_text())
        validate_snapshot(kind, item)
        if item.get("manifest_url") != manifest_url:
            raise StackError(f"{kind} has an unfinished download from another manifest; clear its bootstrap cache to switch")
    else:
        if archive.exists():
            raise StackError(f"{archive} has no pinned manifest; refusing to mix snapshots")
        item = choose_snapshot(kind, manifest_url)
        write_json(descriptor, item)
    workers = positive_int(os.environ.get("DOWNLOAD_WORKERS", "3"), "DOWNLOAD_WORKERS")
    download(item, archive, chunk_bytes, reserve_bytes, workers=workers)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    progress("restoring", kind)
    log(f"Extracting verified {kind} archive")
    try:
        extract(kind, archive, staging, reserve_bytes)
        write_json(staging / ".bootstrap.json", item)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    archive.unlink()
    descriptor.unlink()
    log(f"{kind} bootstrap complete; compressed download removed")


def blockchain_info():
    request = Request(RPC_URL, data=json.dumps({"jsonrpc": "2.0", "id": "zovereign", "method": "getblockchaininfo", "params": []}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        reply = json.load(response)
    if reply.get("error") or not isinstance(reply.get("result"), dict):
        raise StackError("Zakura RPC returned an error")
    info = reply["result"]
    if info.get("chain") != "main" or info.get("pruned") is not False:
        raise StackError("The bundled node must serve mainnet in archive mode")
    return info


def near_tip(info):
    return int(info.get("blocks", 0)) > 0 and int(info.get("estimatedheight", 2**32)) - int(info["blocks"]) <= 5


def grpc(method, data=None):
    # The probe stays on loopback; external clients must validate the server certificate.
    transport = "-insecure" if os.environ.get("TLS_CERT") else "-plaintext"
    args = ["grpcurl", transport, "-max-time", "5"]
    if data is not None:
        args.extend(["-d", json.dumps(data)])
    args.extend(["127.0.0.1:9067", f"cash.z.wallet.sdk.rpc.CompactTxStreamer/{method}"])
    return json.loads(command(args, capture=True, timeout=8, stderr=subprocess.DEVNULL))


def health():
    info = blockchain_info()
    if not near_tip(info):
        raise StackError("Zakura is catching up to the network")
    lightd = grpc("GetLightdInfo")
    if lightd.get("chainName") != "main":
        raise StackError("lightwalletd is not serving mainnet")
    height = max(1, int(info["blocks"]) - 1)
    block = grpc("GetBlock", {"height": height})
    if int(block.get("height", -1)) != height:
        raise StackError("lightwalletd has not caught up to the bundled node")
    return height


def zakura_environment(root):
    # Zakura treats every ZAKURA_* variable as config. Wrapper metadata and
    # snapshot URLs use the same prefix but are not native section__key settings.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("ZAKURA_") or "__" in key}
    # Keep the bundled node's RPC private and prevent accidental network/state overrides.
    env.update({
        "ZAKURA_NETWORK__NETWORK": "Mainnet", "ZAKURA_NETWORK__LISTEN_ADDR": "0.0.0.0:8233",
        "ZAKURA_NETWORK__IDENTITY_DIR": str(root / "identity"),
        "ZAKURA_RPC__LISTEN_ADDR": "127.0.0.1:8232", "ZAKURA_RPC__ENABLE_COOKIE_AUTH": "false",
        "ZAKURA_STATE__CACHE_DIR": str(root / "zakura"), "ZAKURA_STATE__STORAGE_MODE": "archive",
        "ZAKURA_STATE__EPHEMERAL": "false", "ZAKURA_TRACING__USE_COLOR": "false",
    })
    return env


def start_lightwalletd(args):
    # Create stdout as the unprivileged supervisor. lightwalletd reopens
    # /dev/stdout, so inheriting Docker's root-owned pipe directly would fail.
    process = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def forward():
        try:
            for line in process.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        finally:
            process.stdout.close()

    thread = threading.Thread(target=forward, daemon=True)
    LOG_THREADS.append(thread)
    thread.start()
    return process


def supervise(root):
    env = zakura_environment(root)
    log("Starting Zakura; RPC is private at 127.0.0.1:8232")
    zakura = spawn(["zakurad", "start"], env=env)
    waiting_since = time.monotonic()
    last_report = 0
    timeout = positive_int(os.environ.get("RPC_READY_TIMEOUT", "86400"), "RPC_READY_TIMEOUT")
    while True:
        if zakura.poll() is not None:
            raise StackError(f"Zakura exited during startup ({zakura.returncode})")
        try:
            info = blockchain_info()
            if near_tip(info):
                break
            status = f"Zakura catch-up: height {info['blocks']}, estimated tip {info.get('estimatedheight')}"
        except (OSError, ValueError, StackError) as error:
            status = f"Waiting for Zakura RPC: {error}"
        if time.monotonic() - last_report > 30:
            progress("syncing", "zakura", message=status)
            log(status)
            last_report = time.monotonic()
        if time.monotonic() - waiting_since > timeout:
            raise StackError("Zakura did not reach RPC readiness before RPC_READY_TIMEOUT")
        time.sleep(2)
    config = root / "bootstrap" / "zcash.conf"
    config.write_text("rpcbind=127.0.0.1\nrpcport=8232\nrpcuser=lightwalletd\nrpcpassword=none\n")
    tls_cert = os.environ.get("TLS_CERT")
    tls_key = os.environ.get("TLS_KEY")
    if bool(tls_cert) != bool(tls_key):
        raise StackError("Set both TLS_CERT and TLS_KEY, or neither")
    tls = [f"--tls-cert={tls_cert}", f"--tls-key={tls_key}"] if tls_cert else ["--no-tls-very-insecure"]
    progress("starting", "lightwalletd")
    log("Starting snapshot-restored lightwalletd against the bundled Zakura RPC")
    lightwalletd = start_lightwalletd([
        "lightwalletd", "--grpc-bind-addr=0.0.0.0:9067", "--http-bind-addr=127.0.0.1:9068",
        f"--zcash-conf-path={config}", f"--data-dir={root / 'lightwalletd'}",
        "--log-file=/dev/stdout", "--log-level=4", *tls,
    ])
    log("Both processes started; Docker health becomes healthy when compact blocks can be served near the node tip")
    last_health = 0
    while zakura.poll() is None and lightwalletd.poll() is None:
        if time.monotonic() - last_health >= 30:
            try:
                served = health()
                progress("ready", compact_block_height=served)
            except (OSError, ValueError, StackError) as error:
                progress("syncing", message=str(error))
            last_health = time.monotonic()
        time.sleep(1)
    failed = "Zakura" if zakura.poll() is not None else "lightwalletd"
    raise StackError(f"{failed} exited; stopping the other service so Docker can restart the complete stack")


def run():
    global VOLUME_LOCK, STATUS_ROOT
    root = Path(os.environ.get("DATA_DIR", "/data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "bootstrap"
    cache.mkdir(exist_ok=True)
    if os.getuid() == 0:
        for path in (root, cache):
            os.chown(path, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    VOLUME_LOCK = (root / ".stack.lock").open("a")
    try:
        fcntl.flock(VOLUME_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise StackError("Another stack is already using this data volume") from error
    STATUS_ROOT = root
    progress("initializing")
    chunk_bytes = positive_int(os.environ.get("DOWNLOAD_CHUNK_MIB", "256"), "DOWNLOAD_CHUNK_MIB") * 1024 * 1024
    reserve_bytes = positive_int(os.environ.get("DISK_RESERVE_GIB", "20"), "DISK_RESERVE_GIB", minimum=0) * 1024**3
    positive_int(os.environ.get("RPC_READY_TIMEOUT", "86400"), "RPC_READY_TIMEOUT")
    certificate, key = os.environ.get("TLS_CERT"), os.environ.get("TLS_KEY")
    if bool(certificate) != bool(key):
        raise StackError("Set both TLS_CERT and TLS_KEY, or neither")
    if certificate and not all(Path(path).is_file() and os.access(path, os.R_OK) for path in (certificate, key)):
        raise StackError("TLS certificate and key must be readable by UID 10001")
    for kind, url in (("zakura", os.environ.get("ZAKURA_SNAPSHOT_MANIFEST", ZAKURA_MANIFEST)),
                      ("lightwalletd", os.environ.get("LIGHTWALLETD_SNAPSHOT_MANIFEST", LIGHTWALLETD_MANIFEST))):
        bootstrap(kind, root, url, chunk_bytes, reserve_bytes)
    supervise(root)


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in ("run", "health", "status"):
        print("usage: stack.py run|health|status", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        if mode == "status":
            print(json.dumps(status(Path(os.environ.get("DATA_DIR", "/data")))))
        elif mode == "health":
            log(f"Healthy: compact block {health()} served through the bundled node")
        else:
            run()
        return 0
    except Shutdown:
        progress("stopped", message="Services stopped; persistent data retained")
        log("Stopping; persistent databases and completed download ranges are retained")
        return 0
    except (StackError, OSError, ValueError, tarfile.TarError) as error:
        progress("error", message=str(error))
        log(f"ERROR: {error}")
        return 1
    finally:
        # Signal both services together before waiting for their database flushes.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for process in list(CHILDREN):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in list(CHILDREN):
            stop(process)
        for thread in LOG_THREADS:
            thread.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
