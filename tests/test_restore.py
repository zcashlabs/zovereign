"""Behavioral restore regressions: real Bash/zstd/checksum, inert Docker/cloud."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('RESTORE_SCRIPT', ROOT / 'scripts/start-from-snapshot.sh'))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='restore-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        downloads = self.root / 'downloads'
        downloads.mkdir()
        (self.root / 'home').mkdir()
        shim = '#!' + sys.executable + '\n' + (ROOT / 'tests/restore_command.py').read_text()
        for name in ['docker', 'aws', 'curl', 'zstd', 'rm', 'mktemp']:
            path = self.bin / name
            path.write_text(shim)
            path.chmod(0o755)
        self.state = self.root / 'state.json'
        self.state.write_text(json.dumps({'containers': {}, 'volumes': {}}))
        self.trace = self.root / 'trace.jsonl'
        self.trace.touch()
        self.archive = self.root / 'tiny snapshot.tar.zst'
        tar = self.root / 'tiny.tar'
        data = self.root / 'data/db/main'
        data.mkdir(parents=True)
        (data / 'blocks').write_bytes(b'synthetic-block-bytes')
        (data / 'lengths').write_bytes(b'\x00\x00\x00\x15')
        with tarfile.open(tar, 'w') as stream:
            stream.add(self.root / 'data/db', arcname='db')
        real_zstd = shutil.which('zstd')
        self.assertIsNotNone(real_zstd, 'Tests require zstd on PATH')
        subprocess.run([real_zstd, '--quiet', '-o', str(self.archive), str(tar)], check=True)
        self.checksum = Path(str(self.archive) + '.sha256')
        self.checksum.write_text(hashlib.sha256(self.archive.read_bytes()).hexdigest() + '  tiny snapshot.tar.zst\n')
        # The unmodified readiness loop uses a real loopback TCP connection.
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.addCleanup(self.listener.close)
        self.env = {
            'PATH': str(self.bin) + ':' + os.environ['PATH'],
            'HOME': str(self.root / 'home'), 'TMPDIR': str(downloads),
            'RPC_HOST': 'unused-node', 'RPC_PASSWORD': 'public-test-placeholder',
            'GRPC_PORT': str(self.listener.getsockname()[1]), 'START_TIMEOUT': '1',
            'CONTAINER_NAME': 'test-lightwalletd', 'VOLUME_NAME': 'test-cache',
            'IMAGE': 'inert-test-image', 'RESTORE_STATE': str(self.state),
            'RESTORE_TRACE': str(self.trace), 'RESTORE_FAULTS': '{}',
            'REAL_ZSTD': real_zstd, 'ARCHIVE_FIXTURE': str(self.archive),
            'CHECKSUM_FIXTURE': str(self.checksum),
        }

    def run_restore(self, source=None, faults=None, extra=None):
        env = {**self.env, 'RESTORE_FAULTS': json.dumps(faults or {}), **(extra or {})}
        return subprocess.run(['/bin/bash', str(SCRIPT), source or str(self.archive)], env=env, text=True, capture_output=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def resources(self):
        return json.loads(self.state.read_text())

    def assert_empty(self):
        self.assertEqual(self.resources(), {'containers': {}, 'volumes': {}})
        self.assertEqual(list((self.root / 'downloads').iterdir()), [])

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('started successfully', result.stdout)
        self.assertEqual(set(self.resources()['containers']), {'test-lightwalletd'})
        self.assertEqual(set(self.resources()['volumes']), {'test-cache'})
        self.assertEqual(list((self.root / 'downloads').iterdir()), [])

    def test_s3_restore(self):
        result = self.run_restore('s3://fixture-bucket/path/snapshot.tar.zst')
        self.assert_success(result)
        aws = [call for call in self.calls() if call[0] == 'aws']
        self.assertEqual(len(aws), 2)
        self.assertTrue(all('/downloads/' in call[-1] for call in aws))
        self.assertIn('Checksum verified.', result.stdout)

    def test_s3_inherited_workspace_is_untouched(self):
        inherited = self.root / 'unrelated'
        inherited.mkdir()
        (inherited / 'marker').write_text('keep')
        result = self.run_restore('s3://fixture/snapshot.tar.zst', extra={'WORK_DIR': str(inherited)})
        self.assert_success(result)
        self.assertEqual((inherited / 'marker').read_text(), 'keep')
        self.assertEqual(list(inherited.iterdir()), [inherited / 'marker'])

    def test_copy_failure_cleans_resources_and_allows_retry(self):
        self.checksum.unlink()
        result = self.run_restore(faults={'copy': True})
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assert_empty()
        self.assert_success(self.run_restore())

    def test_decompression_failure_cleans_resources(self):
        self.checksum.unlink()
        result = self.run_restore(faults={'decompress': True})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assert_empty()

    def test_rightmost_pipeline_failure_status_is_preserved(self):
        result = self.run_restore(faults={'decompress': True, 'copy': True})
        self.assertEqual(result.returncode, 4)
        self.assert_empty()

    def test_local_archive_and_sidecar_are_preserved(self):
        original = self.archive.read_bytes(), self.checksum.read_bytes()
        self.assert_success(self.run_restore())
        self.assertEqual((self.archive.read_bytes(), self.checksum.read_bytes()), original)
        self.assertFalse(any(c[0] in ['aws', 'curl'] for c in self.calls()))

    def test_http_restore_with_optional_sidecar(self):
        result = self.run_restore('https://fixture.invalid/snapshot.tar.zst', faults={'http_checksum': True})
        self.assert_success(result)
        self.assertIn('continuing without checksum', result.stderr)
        self.assertEqual(len([c for c in self.calls() if c[0] == 'curl']), 2)

    def test_http_restore_with_checksum(self):
        self.assert_success(self.run_restore('https://fixture.invalid/snapshot.tar.zst'))

    def test_local_without_checksum(self):
        self.checksum.unlink()
        self.assert_success(self.run_restore())

    def test_mixed_case_checksum(self):
        self.checksum.write_text(self.checksum.read_text().upper())
        self.assert_success(self.run_restore())

    def test_acquisition_failures_do_not_create_resources(self):
        for fault, source, status in [('s3_archive', 's3://fixture/a', 12), ('s3_checksum', 's3://fixture/a', 12), ('http_archive', 'https://fixture.invalid/a', 22)]:
            with self.subTest(fault=fault):
                result = self.run_restore(source, faults={fault: True})
                self.assertEqual(result.returncode, status, result.stderr)
                self.assert_empty()

    def test_integrity_failures_stop_before_resource_creation(self):
        for checksum in ['not-a-checksum', '0' * 64]:
            with self.subTest(checksum=checksum):
                self.checksum.write_text(checksum + '\n')
                result = self.run_restore()
                self.assertNotEqual(result.returncode, 0)
                self.assert_empty()

    def test_empty_and_invalid_compression(self):
        self.checksum.unlink()
        for data in [b'', b'not-zstd']:
            with self.subTest(data=data):
                self.archive.write_bytes(data)
                self.assertNotEqual(self.run_restore().returncode, 0)
                self.assert_empty()

    def test_failed_structural_check_cleans_resources(self):
        self.assertNotEqual(self.run_restore(faults={'check': True}).returncode, 0)
        self.assert_empty()

    def test_failed_helper_creation_cleans_owned_volume(self):
        self.assertEqual(self.run_restore(faults={'helper_create': True}).returncode, 8)
        self.assert_empty()

    def test_partial_helper_creation_is_recovered(self):
        self.assertEqual(self.run_restore(faults={'helper_create_after': True}).returncode, 8)
        self.assert_empty()

    def test_colliding_foreign_helper_is_preserved(self):
        self.assertEqual(self.run_restore(faults={'helper_race': True}).returncode, 8)
        self.assertEqual(len(self.resources()['containers']), 1)
        self.assertEqual(next(iter(self.resources()['containers'].values()))['marker'], 'keep')
        self.assertEqual(self.resources()['volumes'], {})

    def test_partial_final_start_is_recovered(self):
        self.assertEqual(self.run_restore(faults={'target_start': True}).returncode, 13)
        self.assert_empty()

    def test_partial_validation_start_is_recovered(self):
        self.assertNotEqual(self.run_restore(faults={'check_start': True}).returncode, 0)
        self.assert_empty()

    def test_signals_use_the_same_cleanup(self):
        for sig, status in [('SIGINT', 130), ('SIGTERM', 143)]:
            with self.subTest(signal=sig):
                result = self.run_restore(faults={'signal': sig})
                self.assertEqual(result.returncode, status, result.stderr)
                self.assert_empty()

    def test_cleanup_failures_are_reported_and_original_status_survives(self):
        result = self.run_restore(faults={'copy': True, 'helper_rm': True})
        self.assertEqual(result.returncode, 4)
        self.assertIn('cleanup', result.stderr.lower())
        self.assertIn('helper', result.stderr.lower())
        self.assertTrue(self.resources()['containers'])
        self.assertTrue(self.resources()['volumes'])

    def test_failed_helper_removal_never_reports_success(self):
        result = self.run_restore(faults={'helper_rm': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('started successfully', result.stdout)
        self.assertIn('helper', result.stderr.lower())

    def test_cleanup_query_failure_is_not_reported_as_absence(self):
        result = self.run_restore(faults={'copy': True, 'container_ls': True})
        self.assertEqual(result.returncode, 4)
        self.assertIn('cleanup', result.stderr.lower())

    def test_temporary_cleanup_failure_makes_success_nonzero(self):
        result = self.run_restore(faults={'temp_rm': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('temporary', result.stderr.lower())
        self.assertIn('test-lightwalletd', self.resources()['containers'])

    def test_volume_created_by_another_invocation_is_not_used_or_removed(self):
        result = self.run_restore(faults={'volume_race': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.resources()['volumes']['test-cache']['marker'], 'foreign')
        self.assertFalse(any(c[:2] == ['docker', 'cp'] for c in self.calls()))

    def test_existing_resources_are_preserved_without_force(self):
        for kind, resource in [('containers', 'test-lightwalletd'), ('volumes', 'test-cache')]:
            with self.subTest(kind=kind):
                state = {'containers': {}, 'volumes': {}}
                state[kind][resource] = {'id': 'existing-id', 'labels': {}, 'marker': 'keep'}
                self.state.write_text(json.dumps(state))
                self.assertNotEqual(self.run_restore('s3://fixture/a').returncode, 0)
                self.assertEqual(self.resources(), state)
        self.assertFalse(any(c[0] == 'aws' for c in self.calls()))

    def test_force_removes_only_configured_old_targets(self):
        self.state.write_text(json.dumps({'containers': {'test-lightwalletd': {'id': 'old', 'labels': {}, 'volume': 'test-cache'}, 'protected': {'id': 'protected', 'labels': {}}}, 'volumes': {'test-cache': {'labels': {}}, 'protected-volume': {'labels': {}}}}))
        result = self.run_restore(faults={'copy': True}, extra={'FORCE': '1'})
        self.assertEqual(result.returncode, 4)
        self.assertEqual(set(self.resources()['containers']), {'protected'})
        self.assertEqual(set(self.resources()['volumes']), {'protected-volume'})


if __name__ == '__main__':
    unittest.main()
