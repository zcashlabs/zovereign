#!/usr/bin/env python3
"""Stateful Docker boundary for CLI workflow tests. Never invokes Docker."""
import json
import os
from pathlib import Path
import sys
p = Path(os.environ['FAKE_DOCKER_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
if a[:1] == ['--context']: a = a[2:]
s.setdefault('calls', []).append(a)
s.setdefault('locales', []).append({key: os.environ.get(key) for key in ('LANG', 'LC_ALL')})
def finish(value=None, error=None):
    p.write_text(json.dumps(s))
    if error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    if value is not None: print(json.dumps(value) if not isinstance(value, str) else value)
    raise SystemExit(0)
def find(name):
    return next((c for c in s['containers'].values() if c['Id'] == name or c['Name'].lstrip('/') == name), None)
if a == ['context','show']: finish('fake-vm')
if a[0] == 'info': finish({'ID':s.get('engine','vm-1'),'OSType':'linux','Architecture':s.get('arch','aarch64'),'NCPU':4,'MemTotal':s.get('memory',16*1024**3)})
if a[:2] == ['container','inspect']:
    if s.get('inspect_errors', {}).get('container'): finish(error=s['inspect_errors']['container'])
    c = find(a[2]); finish([c] if c else None, None if c else f'Error: No such container: {a[2]}')
if a[:2] == ['image','inspect']:
    if s.get('inspect_errors', {}).get('image'): finish(error=s['inspect_errors']['image'])
    i = s['images'].get(a[2]); finish([i] if i else None, None if i else f'Error response from daemon: No such image: {a[2]}')
if a[0] == 'pull':
    if s.get('fail_pull'): finish(error='injected registry failure')
    if a[1] in s.get('registry_images', {}):
        import copy
        image = s['registry_images'][a[1]]
        s['images'][a[1]] = copy.deepcopy(image)
        s['images'][image['Id']] = copy.deepcopy(image)
    finish('pulled')
if a[:2] == ['volume','create']: finish(a[2])
if a[0] == 'run': finish({'free_bytes':s.get('free_bytes',800*1024**3),'cpu_compatible':True,'total_bytes':1024**4})
if a[0] in ('start','stop','restart'):
    c = find(a[-1])
    if s.get('fail_start') and a[0]=='start': finish(error='injected start failure')
    if not c: finish(error='No such container')
    c['State']['Running'] = a[0] != 'stop'
    c['State']['Status'] = 'running' if c['State']['Running'] else 'exited'
    finish(c['Id'])
if a[0] == 'update':
    c = find(a[-1]); c['HostConfig']['RestartPolicy']['Name'] = a[a.index('--restart')+1]; finish(c['Id'])
if a[0] == 'create':
    import copy
    c = copy.deepcopy(next(iter(s['containers'].values()),s['template']))
    c.update(Id='replacement', Name='/'+a[a.index('--name')+1], Image=a[-1])
    c['State'] = {'Running':False,'Status':'created'}
    c['HostConfig']['RestartPolicy']['Name'] = 'unless-stopped'
    s['containers']['replacement'] = c
    finish('replacement')
if a[0] == 'exec':
    if a[-1] == 'health': finish('Healthy: compact block 120')
    if 'disk_usage' in a[-1]: finish({'free_bytes':800*1024**3,'cpu_compatible':True,'total_bytes':1024**4})
    finish({'phase':'ready','compact_block_height':120})
if a[0] == 'logs':
    if s.get('log_stderr'): print(s['log_stderr'], file=sys.stderr)
    finish(s.get('log_stdout', 'retained databases'))
if a[0] == 'stats': finish({'CPUPerc':'4%','MemUsage':'1GiB / 16GiB'})
finish(error='Unimplemented fake Docker command: '+repr(a))
