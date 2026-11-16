"""Progress contract tests that need neither Docker nor a chain download."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('stack', Path(os.environ.get('STACK_MODULE', Path(__file__).parents[1] / 'docker/stack.py')))
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)

class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        stack.STATUS_ROOT = self.root
    def tearDown(self):
        stack.STATUS_ROOT = None
        self.temp.cleanup()
    def test_cleanup_accepts_exit_between_poll_and_signal(self):
        child = Mock(pid=9999)
        child.poll.return_value = None
        child.wait.return_value = 0
        with patch.object(stack.os, 'killpg', side_effect=PermissionError('exited')):
            stack.stop(child)
        child.wait.assert_any_call(timeout=0.1)
    def test_cleanup_does_not_ignore_signal_failure_for_live_child(self):
        child = Mock(pid=9999)
        child.poll.return_value = None
        child.wait.side_effect = stack.subprocess.TimeoutExpired('child', 0.1)
        with patch.object(stack.os, 'killpg', side_effect=PermissionError('live')):
            with self.assertRaises(stack.subprocess.TimeoutExpired):
                stack.stop(child)
    def test_atomic_progress_and_live_node_checkpoint(self):
        stack.progress('downloading', 'zakura', completed_bytes=128, total_bytes=256)
        before = (self.root / 'status.json').read_bytes()
        with patch.object(stack, 'blockchain_info', return_value={'blocks': 120, 'estimatedheight': 121, 'bestblockhash': 'a', 'chain': 'main', 'pruned': False}):
            result = stack.status(self.root)
        self.assertEqual(result['completed_bytes'], 128)
        self.assertEqual(result['node']['bestblockhash'], 'a')
        self.assertEqual((self.root / 'status.json').read_bytes(), before)
        self.assertFalse((self.root / 'status.json.tmp').exists())
    def test_probe_before_node_start_reports_progress(self):
        stack.progress('restoring', 'lightwalletd')
        with patch.object(stack, 'blockchain_info', side_effect=OSError('not ready')):
            self.assertEqual(stack.status(self.root)['phase'], 'restoring')
    def test_progress_does_not_break_recovery_when_disk_is_full(self):
        with patch.object(stack, 'write_json', side_effect=OSError('disk full')):
            stack.progress('error', message='original failure')
    def test_phase_changes_do_not_retain_stale_download_fields(self):
        stack.progress('downloading', 'zakura', completed_bytes=128, total_bytes=256)
        stack.progress('ready', compact_block_height=120)
        result = json.loads((self.root / 'status.json').read_text())
        self.assertNotIn('total_bytes', result)
        self.assertEqual(result['phase'], 'ready')

if __name__ == '__main__': unittest.main()
