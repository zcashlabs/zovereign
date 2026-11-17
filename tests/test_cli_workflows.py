"""CLI workflow tests using a fake Docker engine, not mocked application methods."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).parents[1]
BINARY=Path(os.environ.get('ZOVEREIGN_TEST_BINARY',ROOT/'target/debug/zovereign'))
ENV=['ZAKURA_VERSION=1.8.7','ZAKURA_DB_MAJOR=30','LIGHTWALLETD_VERSION=v0.6.0','DATA_DIR=/data']
IMAGE={'Id':'sha256:old','Config':{'Env':ENV,'Entrypoint':['/usr/bin/tini','--','python3','/opt/zovereign/stack.py'],'Cmd':['run']}}
CONTAINER={'Id':'original','Name':'/zovereign','Image':'sha256:old','Config':IMAGE['Config'],'Mounts':[{'Type':'volume','Name':'zovereign-data','Destination':'/data','RW':True}], 'HostConfig':{'NetworkMode':'bridge','RestartPolicy':{'Name':'unless-stopped','MaximumRetryCount':0},'PortBindings':{'9067/tcp':[{'HostIp':'127.0.0.1','HostPort':'9067'}]}},'State':{'Running':True,'Status':'running','Health':{'Status':'healthy'}}}
class Workflows(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'docker.json'
        new=copy.deepcopy(IMAGE);new['Id']='sha256:new'
        self.path.write_text(json.dumps({'images':{'sha256:old':IMAGE,'sha256:new':new,'candidate':new},'containers':{'original':CONTAINER},'template':CONTAINER}))
        self.env={**os.environ,'ZOVEREIGN_DOCKER':str(ROOT/'tests/fake_docker.py'),'FAKE_DOCKER_STATE':str(self.path)}
        self.cli('adopt','--yes')
    def tearDown(self): self.tmp.cleanup()
    def cli(self,*args,ok=True):
        r=subprocess.run([str(BINARY),'--config-dir',str(self.root/'config'),'--json',*args],env=self.env,text=True,capture_output=True)
        if ok:self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        else:self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
        return r
    def state(self):return json.loads(self.path.read_text())
    def save(self,s):self.path.write_text(json.dumps(s))
    def fresh_setup(self):
        import shutil
        config = self.root / 'config'
        if config.exists():
            shutil.rmtree(config)
        s=self.state();s['containers']={};s['calls']=[];self.save(s)
    def test_setup_creates_a_managed_installation(self):
        self.fresh_setup()
        self.cli('setup','--image','candidate','--yes')
        self.assertTrue(self.state()['containers']['replacement']['State']['Running'])
        self.assertTrue((self.root/'config/installation.json').exists())
    def test_failed_start_keeps_recoverable_installation(self):
        self.fresh_setup();s=self.state();s['fail_start']=True;self.save(s)
        self.cli('setup','--image','candidate','--yes',ok=False)
        self.assertTrue((self.root/'config/installation.json').exists())
        s=self.state();s['fail_start']=False;self.save(s)
        self.cli('start')
    def test_setup_rejects_unsupported_vm_and_low_volume_space(self):
        self.fresh_setup();s=self.state();s['arch']='riscv64';self.save(s)
        self.cli('setup','--image','candidate','--yes',ok=False)
        self.assertFalse(any(c[0]=='create' for c in self.state()['calls']))
        s=self.state();s['arch']='aarch64';s['free_bytes']=1024;self.save(s)
        self.cli('setup','--image','candidate','--yes',ok=False)
        self.assertFalse(any(c[0]=='create' for c in self.state()['calls']))
    def test_closed_stdout_pipe_exits_without_a_panic(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            result = subprocess.run([str(BINARY),'--config-dir',str(self.root/'config'),'--json','health'], env=self.env, stdout=write_fd, stderr=subprocess.PIPE, text=True)
        finally:
            os.close(write_fd)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('panicked',result.stderr)
    def test_malformed_configuration_identifies_its_path(self):
        path=self.root/'config/installation.json'
        path.write_text('{broken')
        result=self.cli('status',ok=False)
        self.assertIn(str(path),json.loads(result.stdout)['error'])
    def test_adoption_and_json_controls(self):
        status=json.loads(self.cli('--health').stdout);self.assertTrue(status['ready'])
        self.assertEqual(json.loads(self.cli('connect').stdout)['bindings'][0]['HostPort'],'9067')
    def test_engine_change_refuses_mutation(self):
        s=self.state();s['engine']='other';self.save(s)
        self.cli('stop',ok=False)
        self.assertTrue(self.state()['containers']['original']['State']['Running'])



    def test_setup_supports_native_linux_architecture_names(self):
        for architecture in ('x86_64', 'amd64', 'aarch64', 'arm64'):
            with self.subTest(architecture=architecture):
                self.fresh_setup()
                state = self.state()
                state['arch'] = architecture
                self.save(state)
                self.cli('setup', '--image', 'candidate', '--yes')
                self.assertTrue(self.state()['containers']['replacement']['State']['Running'])

    def test_setup_rejects_unsupported_versions_before_creating_storage(self):
        for key, value in (('ZAKURA_VERSION', '2.0.0'), ('ZAKURA_DB_MAJOR', '31'),
                           ('LIGHTWALLETD_VERSION', 'v99.0.0')):
            with self.subTest(key=key):
                self.fresh_setup()
                state = self.state()
                state['images']['candidate']['Config']['Env'] = [
                    f'{key}={value}' if item.startswith(key + '=') else item for item in ENV]
                self.save(state)
                self.cli('setup', '--image', 'candidate', '--yes', ok=False)
                self.assertFalse((self.root / 'config/installation.json').exists())
                self.assertFalse(any(call[0] == 'create' or call[:2] == ['volume', 'create']
                                     for call in self.state()['calls']))

    def test_adoption_rejects_unsupported_versions_without_changing_runtime(self):
        import shutil
        for key, value in (('ZAKURA_VERSION', '2.0.0'), ('ZAKURA_DB_MAJOR', '31'),
                           ('LIGHTWALLETD_VERSION', 'v99.0.0')):
            with self.subTest(key=key):
                shutil.rmtree(self.root / 'config')
                state = self.state()
                state['containers']['original']['Config']['Env'] = [
                    f'{key}={value}' if item.startswith(key + '=') else item for item in ENV]
                self.save(state)
                self.cli('adopt', '--yes', ok=False)
                self.assertFalse((self.root / 'config/installation.json').exists())
                self.assertTrue(self.state()['containers']['original']['State']['Running'])
                self.assertFalse(any(call[0] in ('start', 'stop', 'rm', 'create')
                                     for call in self.state()['calls']))

    def test_setup_refreshes_a_cached_tag_and_pins_the_pulled_image(self):
        self.fresh_setup()
        state = self.state()
        state['images']['candidate'] = copy.deepcopy(IMAGE)
        fresh = copy.deepcopy(IMAGE)
        fresh['Id'] = 'sha256:fresh'
        state['registry_images'] = {'candidate': fresh}
        self.save(state)
        self.cli('setup', '--image', 'candidate', '--yes')
        state = self.state()
        self.assertIn(['pull', 'candidate'], state['calls'])
        self.assertEqual(state['containers']['replacement']['Image'], 'sha256:fresh')
        self.assertEqual(json.loads((self.root / 'config/installation.json').read_text())['image'],
                         'sha256:fresh')
        self.assertEqual(next(call for call in state['calls'] if call[0] == 'create')[-1],
                         'sha256:fresh')

    def test_setup_does_not_fall_back_to_cache_after_a_failed_pull(self):
        self.fresh_setup()
        state = self.state()
        state['fail_pull'] = True
        self.save(state)
        self.cli('setup', '--image', 'candidate', '--yes', ok=False)
        state = self.state()
        self.assertIn(['pull', 'candidate'], state['calls'])
        self.assertFalse(any(call[0] == 'create' or call[:2] == ['volume', 'create']
                             for call in state['calls']))
        self.assertFalse((self.root / 'config/installation.json').exists())

    def test_setup_local_image_never_pulls_and_requires_a_cached_image(self):
        self.fresh_setup()
        state = self.state()
        state['fail_pull'] = True
        self.save(state)
        self.cli('setup', '--local-image', '--image', 'candidate', '--yes')
        self.assertFalse(any(call[0] == 'pull' for call in self.state()['calls']))
        self.fresh_setup()
        state = self.state()
        del state['images']['candidate']
        self.save(state)
        self.cli('setup', '--local-image', '--image', 'candidate', '--yes', ok=False)
        self.assertFalse(any(call[0] in ('pull', 'create') or call[:2] == ['volume', 'create']
                             for call in self.state()['calls']))

    def test_configured_setup_and_adoption_fail_before_confirmation(self):
        record = (self.root / 'config/installation.json').read_bytes()
        before = self.state()['calls']
        for command in ('adopt', 'setup'):
            with self.subTest(command=command):
                result = self.cli(command, ok=False)
                self.assertIn('already configured', json.loads(result.stdout)['error'])
                self.assertNotIn('Run interactively', result.stdout)
                self.assertNotIn('[y/N]', result.stderr)
                self.assertEqual(self.state()['calls'], before)
                self.assertEqual((self.root / 'config/installation.json').read_bytes(), record)

    def test_docker_uses_a_stable_locale_for_captured_and_streamed_commands(self):
        self.env.update(LANG='fr_FR.UTF-8', LC_ALL='fr_FR.UTF-8')
        state = self.state()
        state['locales'] = []
        self.save(state)
        self.cli('status')
        self.cli('logs')
        locales = self.state()['locales']
        self.assertTrue(locales)
        self.assertTrue(all(value == {'LANG': 'C', 'LC_ALL': 'C'} for value in locales), locales)

    def test_unrelated_inspect_failures_never_trigger_setup(self):
        for message in ('Error response from daemon: Docker context test not found',
                        'Cannot connect to the daemon: No such file or directory',
                        'SSH transport failed: No such image: candidate'):
            with self.subTest(message=message):
                self.fresh_setup()
                state = self.state()
                state['inspect_errors'] = {'container': message}
                self.save(state)
                result = self.cli('setup', '--image', 'candidate', '--yes', ok=False)
                self.assertIn(message, json.loads(result.stdout)['error'])
                self.assertFalse(any(call[0] in ('pull', 'create', 'start') or
                                     call[:2] == ['volume', 'create'] for call in self.state()['calls']))
                self.assertFalse((self.root / 'config/installation.json').exists())

    def test_first_run_and_explicit_setup_use_identical_defaults(self):
        observations = []
        for command in ((), ('setup',)):
            self.fresh_setup()
            state = self.state()
            state['images']['ghcr.io/zcashlabs/zovereign:latest'] = copy.deepcopy(IMAGE)
            self.save(state)
            self.cli(*command, '--yes')
            observations.append((self.state()['calls'],
                                 json.loads((self.root / 'config/installation.json').read_text())))
        self.assertEqual(observations[0], observations[1])

if __name__=='__main__':unittest.main()
