"""Opt-in Docker lifecycle smoke test using tiny synthetic cache files.

No real lightwalletd, chain, cloud, or credentials are used. The service fixture
only opens a TCP listener; Docker/cloud boundaries differ from test_restore.py.
"""

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid


ROOT = Path(__file__).resolve().parents[1]
LABEL = 'zovereign.qa.run'
WRAPPER = r'''
import json, os, pathlib, shutil, subprocess, sys
name=pathlib.Path(sys.argv[0]).name
a=sys.argv[1:]
with open(os.environ['SMOKE_TRACE'], 'a') as f: f.write(json.dumps([name]+a)+'\n')
if name=='aws':
    checksum=a[-2].endswith('.sha256')
    shutil.copyfile(os.environ['SMOKE_CHECKSUM' if checksum else 'SMOKE_ARCHIVE'],a[-1])
    sys.exit(0)
if name=='zstd':
    rc=subprocess.run([os.environ['SMOKE_ZSTD']]+a).returncode
    sys.exit(3 if '--decompress' in a and os.environ.get('SMOKE_DECOMP_FAIL')=='1' else rc)
if a[0] in ('create','run'):
    a[1:1]=['--pull=never','--label','zovereign.qa.run='+os.environ['SMOKE_RUN']]
    # Production defaults are unchanged. Test listeners are loopback-only.
    if '--publish' in a:
        i=a.index('--publish')+1
        a[i]='127.0.0.1:'+a[i]
elif a[:2]==['volume','create']:
    a[2:2]=['--label','zovereign.qa.run='+os.environ['SMOKE_RUN']]
os.execv(os.environ['SMOKE_DOCKER'],[os.environ['SMOKE_DOCKER']]+a)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image', required=True, help='Cached image containing sh, tar and openssl; never pulled')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-script', type=Path, help='Optional original script for the leak negative control')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run = 'restore-' + uuid.uuid4().hex[:12]
    image = 'zovereign-restore-fixture:' + run
    real_docker, real_zstd = shutil.which('docker'), shutil.which('zstd')
    assert real_docker and real_zstd, 'docker and zstd must be installed'
    results = []

    def docker(*command, check=True):
        return subprocess.run([real_docker, *command], text=True, capture_output=True, check=check, timeout=120)

    def owned(kind):
        command = ['container', 'ls', '--all', '--quiet'] if kind == 'container' else ['volume', 'ls', '--quiet']
        return docker(*command, '--filter', f'label={LABEL}={run}').stdout.split()

    def cleanup_resources():
        for identity in owned('container'):
            item = json.loads(docker('container', 'inspect', identity).stdout)[0]
            assert item['Config']['Labels'][LABEL] == run
            docker('container', 'rm', '--force', identity)
        for volume in owned('volume'):
            item = json.loads(docker('volume', 'inspect', volume).stdout)[0]
            assert item['Labels'][LABEL] == run
            docker('volume', 'rm', volume)

    def guard():
        assert shutil.disk_usage(output).free > 45 * 1024**3, '45 GiB disk guard reached'

    protected = docker('ps', '--quiet').stdout.split()
    base = json.loads(docker('image', 'inspect', args.base_image).stdout)[0]['Id']
    image_built = False
    try:
        with tempfile.TemporaryDirectory(prefix=run + '-') as temporary:
            work = Path(temporary)
            context = work / 'image'
            context.mkdir()
            (context / 'entrypoint.sh').write_text('#!/bin/sh\nset -eu\ntest "$(cat /var/lib/lightwalletd/db/main/blocks)" = synthetic-block-bytes\nexec openssl s_server -accept 9067 -nocert -quiet\n')
            (context / 'Dockerfile').write_text(f'FROM {args.base_image}\nCOPY entrypoint.sh /fixture-entrypoint.sh\nENTRYPOINT ["/bin/sh", "/fixture-entrypoint.sh"]\n')
            guard()
            build = docker('build', '--pull=false', '--network=none', '--label', f'{LABEL}={run}', '-t', image, str(context))
            (output / 'build.log').write_text(build.stdout + build.stderr)
            image_built = True
            fixture_image = json.loads(docker('image', 'inspect', image).stdout)[0]['Id']
            bin_dir = work / 'bin'
            bin_dir.mkdir()
            for name in ['docker', 'aws', 'zstd']:
                shim = bin_dir / name
                shim.write_text('#!' + sys.executable + '\n' + WRAPPER)
                shim.chmod(0o755)
            files = work / 'files'
            files.mkdir()
            data = work / 'data/db/main'
            data.mkdir(parents=True)
            (data / 'blocks').write_bytes(b'synthetic-block-bytes')
            (data / 'lengths').write_bytes(b'\x00\x00\x00\x15')
            tar = files / 'valid.tar'
            with tarfile.open(tar, 'w') as stream:
                stream.add(work / 'data/db', arcname='db')
            valid = files / 'valid.tar.zst'
            invalid = files / 'invalid.tar.zst'
            subprocess.run([real_zstd, '-q', '-o', str(valid), str(tar)], check=True)
            bogus = files / 'invalid.tar'
            bogus.write_bytes(b'this is not a tar archive')
            subprocess.run([real_zstd, '-q', '-o', str(invalid), str(bogus)], check=True)
            for archive in [valid, invalid]:
                Path(str(archive) + '.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
            baseline_invalid = files / 'baseline-invalid.tar.zst'
            shutil.copyfile(invalid, baseline_invalid)  # No sidecar: baseline Bash 3.2 cannot lowercase hashes.

            def case(name, archive, mode='local', baseline=False, decompress=False, expect_success=False, target_suffix=None):
                guard()
                target = run + '-' + (target_suffix or name)
                trace = output / (name + '-trace.jsonl')
                trace.touch()
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    port = probe.getsockname()[1]
                env = {k: v for k, v in os.environ.items() if not k.startswith(('AWS_', 'GH_', 'GITHUB_'))}
                env.pop('WORK_DIR', None)
                env.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
                env.update(PATH=str(bin_dir) + ':' + env['PATH'], IMAGE=image, CONTAINER_NAME=target, VOLUME_NAME=target+'-data', GRPC_PORT=str(port), RPC_HOST='unused.invalid', RPC_PASSWORD='public-test-placeholder', DOCKER_NETWORK='', START_TIMEOUT='10', FORCE='0', SMOKE_RUN=run, SMOKE_DOCKER=real_docker, SMOKE_ZSTD=real_zstd, SMOKE_TRACE=str(trace), SMOKE_ARCHIVE=str(archive), SMOKE_CHECKSUM=str(archive)+'.sha256', SMOKE_DECOMP_FAIL='1' if decompress else '0')
                source = str(archive)
                server = None
                if mode == 's3':
                    source = 's3://inert-fixture/snapshot.tar.zst'
                elif mode == 'http':
                    class QuietHandler(http.server.SimpleHTTPRequestHandler):
                        def __init__(self, *a, **kw):
                            super().__init__(*a, directory=str(files), **kw)
                        def log_message(self, *_args):
                            pass
                    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    source = f'http://127.0.0.1:{server.server_port}/{archive.name}'
                script = args.baseline_script if baseline else ROOT / 'scripts/start-from-snapshot.sh'
                try:
                    result = subprocess.run(['/bin/bash', str(script), source], env=env, text=True, capture_output=True, timeout=60)
                finally:
                    if server:
                        server.shutdown()
                        server.server_close()
                (output / (name + '.log')).write_text(result.stdout + result.stderr)
                containers, volumes = owned('container'), owned('volume')
                record = {'case': name, 'exit': result.returncode, 'remaining_containers': len(containers), 'remaining_volumes': len(volumes)}
                if baseline:
                    assert result.returncode != 0 and len(containers) == len(volumes) == 1, record
                    refusal = docker('volume', 'rm', target + '-data', check=False)
                    assert refusal.returncode != 0, 'Real Docker unexpectedly removed an attached volume'
                    record['actual_attached_volume_removal_refused'] = True
                elif expect_success:
                    assert result.returncode == 0 and len(containers) == len(volumes) == 1, result.stdout + result.stderr
                    item = json.loads(docker('container', 'inspect', target).stdout)[0]
                    assert item['Config']['Image'] == image and item['NetworkSettings']['Ports']['9067/tcp'][0]['HostIp'] == '127.0.0.1'
                    digest = docker('exec', target, 'sha256sum', '/var/lib/lightwalletd/db/main/blocks').stdout.split()[0]
                    assert digest == hashlib.sha256(b'synthetic-block-bytes').hexdigest()
                    record['restored_bytes_match'] = True
                    if mode == 's3':
                        assert sum(json.loads(line)[0] == 'aws' for line in trace.read_text().splitlines()) == 2
                else:
                    assert result.returncode == (3 if decompress else 1), result.stdout + result.stderr
                    assert not containers and not volumes, record
                results.append(record)
                (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
                cleanup_resources()

            if args.baseline_script:
                case('baseline-invalid-tar', baseline_invalid, baseline=True)
            case('invalid-tar-cleanup', invalid, target_suffix='retry')
            case('same-name-retry', valid, target_suffix='retry', expect_success=True)
            case('decompression-cleanup', valid, decompress=True)
            case('s3-restore', valid, mode='s3', expect_success=True)
            case('http-restore', valid, mode='http', expect_success=True)
            (output / 'artifact.json').write_text(json.dumps({'base_image': base, 'fixture_image': fixture_image, 'run': run, 'synthetic_cache': True, 'real_lightwalletd_or_chain': False, 'real_aws': False, 'test_only_publish_override': '127.0.0.1', 'source_sha256': hashlib.sha256((ROOT/'scripts/start-from-snapshot.sh').read_bytes()).hexdigest()}, indent=2)+'\n')
    finally:
        cleanup_resources()
        if image_built:
            item = json.loads(docker('image', 'inspect', image).stdout)[0]
            assert item['Config']['Labels'][LABEL] == run
            docker('image', 'rm', image)
        running = set(docker('ps', '--quiet').stdout.split())
        missing = [identity for identity in protected if identity not in running]
        cleanup = {'remaining_containers': owned('container'), 'remaining_volumes': owned('volume'), 'protected_containers': len(protected), 'missing_protected': missing, 'free_gib': round(shutil.disk_usage(output).free / 1024**3, 2)}
        (output / 'cleanup.json').write_text(json.dumps(cleanup, indent=2)+'\n')
        assert not missing and not cleanup['remaining_containers'] and not cleanup['remaining_volumes'], cleanup
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
