"""Privacy primitives without a local Hermes installation."""
import ast
import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('journal', ROOT / 'src/model_switcher/journal.py')
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)
# Exercise the actual sanitizer source without importing the host-only router.
tree = ast.parse((ROOT / 'dashboard/plugin_api.py').read_text())
function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_sanitize_public_url')
namespace = {'Any': object}
exec(compile(ast.Module(body=[function], type_ignores=[]), 'plugin_api.py', 'exec'), namespace)
sanitize = namespace['_sanitize_public_url']


class PrimitiveTests(unittest.TestCase):
    def test_deleted_worktree_file_still_scans_index(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", folder], check=True, capture_output=True)
            secret_file = Path(folder) / "example.txt"
            secret_file.write_text("sk-" + "x" * 25)
            subprocess.run(["git", "-C", folder, "add", "example.txt"], check=True)
            secret_file.unlink()
            result = subprocess.run([sys.executable, str(ROOT / "scripts/privacy_guard.py")], cwd=folder, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("api-key-literal", result.stdout)
            self.assertNotIn("READ-ERROR", result.stdout)
            self.assertNotIn("x" * 25, result.stdout)

    def test_url_credentials_and_queries_are_removed(self):
        self.assertEqual(sanitize('https://user:private@example.invalid:443/v1?token=private#private'), 'https://example.invalid:443/v1')
        self.assertEqual(sanitize('http://[::1]:8080/v1'), 'http://[::1]:8080/v1')

    def test_schemeless_urls_remain_visibly_set(self):
        for value in ("localhost:8080/v1", "api.example.com/v1"):
            self.assertEqual(sanitize(value), "<unparseable URL>")
        self.assertEqual(sanitize(""), "")

    def test_malformed_urls_never_echo_input(self):
        for value in ('https://user:private@[invalid?token=private', 'https://user:private@example.invalid:bad', 'user:private@host?token=private'):
            self.assertNotIn('private', sanitize(value))

    def test_malformed_port_is_unparseable_never_raw_passthrough(self):
        # A bad or out-of-range port (parts.port raises ValueError) must map to
        # the fixed sentinel, never a raw echo of the original input.
        for value in ('https://user:private@example.invalid:bad',
                      'https://host.example:99999/v1',
                      'https://user:private@[invalid?token=private'):
            out = sanitize(value)
            self.assertEqual(out, '<unparseable URL>')
            self.assertNotEqual(out, value)

    def test_permissions_failure_prevents_sqlite_open(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(journal.os, 'chmod', side_effect=PermissionError), patch.object(journal.sqlite3, 'connect') as connect:
                with self.assertRaises(PermissionError):
                    journal.OperationJournal(str(Path(folder) / 'private' / 'lab.db'))
                connect.assert_not_called()

    def test_database_permission_failure_prevents_sqlite_open(self):
        with tempfile.TemporaryDirectory() as folder:
            real_chmod = journal.os.chmod
            def chmod(path, mode):
                if str(path).endswith('.db'):
                    raise PermissionError('synthetic denial')
                real_chmod(path, mode)
            with patch.object(journal.os, 'chmod', side_effect=chmod), patch.object(journal.sqlite3, 'connect') as connect:
                with self.assertRaises(PermissionError):
                    journal.OperationJournal(str(Path(folder) / 'private' / 'lab.db'))
                connect.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits required')
    def test_private_database_and_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'private' / 'lab.db'
            instance = journal.OperationJournal(str(db))
            try:
                self.assertEqual(db.stat().st_mode & 0o777, 0o600)
                self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
            finally:
                instance.close()


if __name__ == '__main__':
    unittest.main()
