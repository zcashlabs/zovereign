"""Inert external commands for test_restore.py; never contact Docker or AWS."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['RESTORE_TRACE'], 'a') as trace:
    trace.write(json.dumps([name, *args]) + '\n')
faults = json.loads(os.environ['RESTORE_FAULTS'])


def option(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


if name == 'mktemp':
    print(tempfile.mkdtemp(prefix='tmp.', dir=os.environ['TMPDIR']))
    sys.exit(0)

if name == 'zstd':
    if '--test' in args and faults.get('archive_test'):
        sys.exit(7)
    result = subprocess.run([os.environ['REAL_ZSTD'], *args], check=False)
    sys.exit(3 if '--decompress' in args and faults.get('decompress') else result.returncode)

if name == 'aws':
    checksum = args[-2].endswith('.sha256')
    if faults.get('s3_checksum' if checksum else 's3_archive'):
        sys.exit(12)
    shutil.copyfile(os.environ['CHECKSUM_FIXTURE' if checksum else 'ARCHIVE_FIXTURE'], args[-1])
    sys.exit(0)

if name == 'curl':
    checksum = args[-1].endswith('.sha256')
    if faults.get('http_checksum' if checksum else 'http_archive'):
        sys.exit(22)
    shutil.copyfile(os.environ['CHECKSUM_FIXTURE' if checksum else 'ARCHIVE_FIXTURE'], option('--output'))
    sys.exit(0)

if name == 'rm':
    if faults.get('temp_rm') and any('/downloads/' in arg for arg in args):
        print('injected temporary-file removal failure', file=sys.stderr)
        sys.exit(5)
    os.execv('/bin/rm', ['rm', *args])

assert name == 'docker', name
state_path = Path(os.environ['RESTORE_STATE'])
state = json.loads(state_path.read_text())
containers = state['containers']
volumes = state['volumes']


def finish(code=0, output=''):
    state_path.write_text(json.dumps(state))
    if output:
        print(output)
    sys.exit(code)


def labels():
    return dict(arg.split('=', 1) for i, arg in enumerate(args) if i and args[i - 1] == '--label')


def matches(item):
    for i, arg in enumerate(args):
        if i and args[i - 1] == '--filter' and arg.startswith('label='):
            key, value = arg[6:].split('=', 1)
            if item['labels'].get(key) != value:
                return False
    return True


if args[:2] == ['container', 'ls']:
    if faults.get('container_ls'):
        finish(11)
    finish(output='\n'.join(c['id'] for c in containers.values() if matches(c)))
if args[:2] == ['volume', 'ls']:
    finish(output='\n'.join(n for n, v in volumes.items() if matches(v)))
if args[:2] == ['volume', 'inspect']:
    volume = volumes.get(args[-1])
    if volume is None:
        finish(1)
    finish(output=volume['labels'].get('org.zcashlabs.zovereign.restore', '') if '--format' in args else '{}')
if args[0] == 'inspect':
    container = containers.get(args[-1])
    if container is None:
        finish(1)
    finish(output=str(container.get('running', False)).lower() if '--format' in args else '{}')
if args[:2] == ['volume', 'create']:
    volume_name = args[-1]
    if faults.get('volume_race'):
        volumes[volume_name] = {'labels': {}, 'marker': 'foreign'}
    volumes.setdefault(volume_name, {'labels': labels()})
    finish(output=volume_name)
if args[:2] == ['volume', 'rm']:
    volume_name = args[-1]
    if faults.get('volume_rm') or any(c.get('volume') == volume_name for c in containers.values()):
        print('volume removal failed: volume is in use or unavailable', file=sys.stderr)
        finish(9)
    volumes.pop(volume_name, None)
    finish()
if args[0] == 'create' or args[0] == 'run':
    is_check = '--entrypoint' in args
    container_name = option('--name', 'unnamed-check')
    role = labels().get('org.zcashlabs.zovereign.role', 'check' if is_check else 'helper' if args[0] == 'create' else 'target')
    if faults.get('helper_create') and role == 'helper':
        finish(8)
    if faults.get('helper_race') and role == 'helper':
        containers[container_name] = {'id': 'foreign-id', 'labels': {}, 'marker': 'keep'}
        finish(8)
    if container_name in containers:
        finish(8)
    containers[container_name] = {
        'id': hashlib.sha256(container_name.encode()).hexdigest(),
        'labels': labels(),
        'volume': option('--volume', '').split(':')[0],
        'running': args[0] == 'run',
    }
    if role == 'helper' and faults.get('helper_create_after'):
        finish(8)
    if is_check:
        if not faults.get('check_start'):
            containers.pop(container_name)
        finish(6 if faults.get('check') or faults.get('check_start') else 0)
    if role == 'target' and faults.get('target_start'):
        containers[container_name]['running'] = False
        finish(13)
    finish(output=containers[container_name]['id'])
if args[0] == 'cp':
    sys.stdin.buffer.read()
    if faults.get('signal'):
        os.kill(os.getppid(), getattr(signal, faults['signal']))
    finish(4 if faults.get('copy') else 0)
if args[0] == 'rm' or args[:2] == ['container', 'rm']:
    identity = args[-1]
    for key, container in list(containers.items()):
        if key == identity or container['id'] == identity:
            if faults.get('helper_rm') and container['labels'].get('org.zcashlabs.zovereign.role') == 'helper':
                print('helper removal failed', file=sys.stderr)
                finish(10)
            containers.pop(key)
    finish()
if args[0] == 'logs':
    finish()
raise AssertionError(f'Unexpected Docker command: {args}')
