"""Standalone privacy guard regressions; run with unittest, without Hermes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

GUARD = Path(__file__).resolve().parents[1] / 'scripts' / 'privacy_guard.py'
spec = importlib.util.spec_from_file_location('privacy_guard', GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.previous = Path.cwd()
        os.chdir(self.repo)
        self.git('init', '-q')

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True)

    def run_guard(self, *args):
        return subprocess.run([sys.executable, str(GUARD), *args], capture_output=True, text=True)

    def test_readme_credentials_are_scanned_without_values(self):
        value = 'sk-' + 'synthetic' * 4
        Path('README.md').write_text(value)
        self.git('add', 'README.md')
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn('api-key-literal', result.stdout)
        self.assertNotIn(value, result.stdout + result.stderr)

    def test_clean_worktree_cannot_hide_staged_secret(self):
        Path('sample.txt').write_text('sk-' + 'synthetic' * 4)
        self.git('add', 'sample.txt')
        Path('sample.txt').write_text('clean')
        for args in ((), ('--staged-only',)):
            result = self.run_guard(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn('api-key-literal', result.stdout)

    def test_staged_only_ignores_unstaged_secret(self):
        Path('sample.txt').write_text('clean')
        self.git('add', 'sample.txt')
        Path('sample.txt').write_text('sk-' + 'synthetic' * 4)
        self.assertEqual(self.run_guard('--staged-only').returncode, 0)
        self.assertEqual(self.run_guard().returncode, 1)

    def test_missing_tracked_file_fails_closed(self):
        Path('sample.txt').write_text('clean')
        self.git('add', 'sample.txt')
        Path('sample.txt').unlink()
        result = self.run_guard()
        self.assertEqual(result.returncode, 2)
        self.assertIn('READ-ERROR', result.stdout)

    def test_staged_deletion_does_not_read_absent_blob(self):
        Path('sample.txt').write_text('clean')
        self.git('add', 'sample.txt')
        self.git('-c', 'user.name=Lab', '-c', 'user.email=lab@example.invalid', 'commit', '-qm', 'Lab fixture')
        self.git('rm', 'sample.txt')
        self.assertEqual(self.run_guard('--staged-only').returncode, 0)

    def test_git_read_error_fails_closed(self):
        with self.assertRaises(OSError):
            guard.read_bytes('absent.txt', ':')
        with patch.object(guard, 'read_bytes', side_effect=PermissionError):
            self.assertIn('READ-ERROR', guard.scan(['sample.txt'])[0])


if __name__ == '__main__':
    unittest.main()
