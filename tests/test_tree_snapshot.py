import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tools import tree_snapshot as tree


class TreeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'sessions' / 'empty').mkdir(parents=True)
        (self.source / '.hidden').mkdir()
        (self.source / '.hidden' / 'config').write_text('synthetic', encoding='utf-8')
        (self.source / 'sessions' / 'many').mkdir()
        for n in range(120):
            (self.source / 'sessions' / 'many' / str(n)).write_text(str(n), encoding='utf-8')
        self.db = self.source / 'sessions' / 'fixture.db'
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('create table sample (value text)')
            db.execute("insert into sample values ('synthetic')")
            db.commit()
        entries = [{'path': name, 'kind': ('sqlite' if name == 'sessions/fixture.db' else kind)}
                   for name, kind in tree.scan(self.source).items()]
        self.inventory = {'version': 1, 'entries': entries}
        self.output = self.root / 'snapshot'

    def create(self):
        tree.create(self.source, self.output, self.inventory)

    def test_roundtrip_many_files_hidden_empty_and_sqlite(self):
        original = tree.scan(self.source)
        source_hash = {name: tree.fingerprint(self.source / name)
                       for name, kind in original.items() if kind == 'file'}
        self.create()
        tree.verify(self.output)
        self.assertEqual(original, tree.scan(self.source))
        self.assertEqual(source_hash, {name: tree.fingerprint(self.source / name)
                                      for name in source_hash})
        restored = self.root / 'restored'
        tree.restore(self.output, restored)
        self.assertEqual(original, tree.scan(restored))
        for name in source_hash:
            if name != 'sessions/fixture.db':
                self.assertEqual((self.source / name).read_bytes(), (restored / name).read_bytes())
        with closing(sqlite3.connect(restored / 'sessions/fixture.db')) as db:
            self.assertEqual(db.execute('select value from sample').fetchall(), [('synthetic',)])
        for name in original:
            self.assertEqual((restored / name).stat().st_mode & 0o777,
                             0o700 if original[name] == 'dir' else 0o600)

    def test_unknown_missing_and_unlisted_empty_dir_rejected(self):
        for name in ('unexpected', '.hidden/unlisted', 'sessions/empty/newdir'):
            path = self.source / name
            path.mkdir() if name.endswith('newdir') else path.write_text('extra')
            with self.assertRaises(ValueError):
                self.create()
            path.rmdir() if path.is_dir() else path.unlink()
        (self.source / '.hidden/config').unlink()
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_links_special_and_sidecars_rejected(self):
        link = self.source / 'link'
        for mode in ('symlink', 'hardlink', 'fifo', 'wal', 'emptyshm'):
            path = link
            if mode == 'symlink':
                link.symlink_to('/tmp')
            elif mode == 'hardlink':
                os.link(self.db, link)
            elif mode == 'fifo':
                os.mkfifo(link)
            else:
                path = Path(str(self.db) + ('-wal' if mode == 'wal' else '-shm'))
                path.write_bytes(b'wal' if mode == 'wal' else b'')
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.create()
            path.unlink()
        self.assertFalse(self.output.exists())

    def test_live_wal_rejected_without_touching_any_source_file(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('pragma journal_mode=wal')
            db.execute("insert into sample values ('wal')")
            db.commit()
            wal = Path(str(self.db) + '-wal')
            self.assertGreater(wal.stat().st_size, 0)
            before = {p.name: p.read_bytes() for p in self.db.parent.iterdir() if p.is_file()}
            with self.assertRaises(ValueError):
                self.create()
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.db.parent.iterdir()
                                      if p.is_file()})
            self.assertFalse(self.output.exists())

    def test_unlisted_invalid_and_misclassified_database(self):
        for path in ('../bad', 'sessions//bad', '/bad', '.incomplete'):
            inventory = {'version': 1, 'entries': [{'path': path, 'kind': 'file'}]}
            with self.subTest(path=path), self.assertRaises(ValueError):
                tree.create(self.source, self.output, inventory)
        inventory = {'version': 1, 'entries': [dict(e) for e in self.inventory['entries']]}
        for item in inventory['entries']:
            if item['path'] == 'sessions/fixture.db':
                item['kind'] = 'file'
        with self.assertRaises(ValueError):
            tree.create(self.source, self.output, inventory)
        self.assertFalse(self.output.exists())

    def test_existing_nested_and_unsafe_parent_rejected(self):
        self.output.mkdir()
        (self.output / 'keep').write_text('keep')
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual((self.output / 'keep').read_text(), 'keep')
        with self.assertRaises(ValueError):
            tree.create(self.source, self.source / 'nested', self.inventory)
        insecure = self.root / 'insecure'
        insecure.mkdir()
        insecure.chmod(0o755)
        with self.assertRaises(ValueError):
            tree.create(self.source, insecure / 'output', self.inventory)

    def test_partial_failure_and_concurrent_change_cleanup(self):
        copy = tree.shutil.copyfile

        def changed(src, dst):
            result = copy(src, dst)
            (self.source / 'new-file').write_text('changed')
            return result

        with patch.object(tree.shutil, 'copyfile', side_effect=changed):
            with self.assertRaises(ValueError):
                self.create()
        self.assertFalse(self.output.exists())
        (self.source / 'new-file').unlink()
        with patch.object(tree, 'isolated_backup', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse(self.output.exists())

    def test_tampering_and_unfinished_output_rejected(self):
        self.create()
        target = self.output / tree.DATA / '.hidden/config'
        target.write_text('tampered')
        with self.assertRaises(ValueError):
            tree.restore(self.output, self.root / 'restore')
        self.assertFalse((self.root / 'restore').exists())
        target.write_text('synthetic')
        (self.output / tree.MARKER).write_text('incomplete')
        with self.assertRaises(ValueError):
            tree.verify(self.output)
        (self.output / tree.MARKER).unlink()
        (self.output / tree.DATA / 'extra').mkdir()
        with self.assertRaises(ValueError):
            tree.verify(self.output)

    def test_inventory_is_unreviewed_private_and_requires_classification(self):
        name = '.hidden/synthetic-secret-name'
        (self.source / name).write_text('synthetic')
        before = tree.scan(self.source)
        path = self.root / 'inventory.json'
        command = [sys.executable, str(Path(tree.__file__)), 'inventory', str(self.source),
                   str(path), '--offline-confirmed']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('synthetic-secret-name', result.stdout + result.stderr)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        inventory = json.loads(path.read_text())
        actual = {item['path']: item['kind'] for item in inventory['entries']}
        self.assertEqual(actual, {p: 'dir' if k == 'dir' else 'unreviewed'
                                  for p, k in before.items()})
        self.assertEqual(before, tree.scan(self.source))
        with self.assertRaises(ValueError):
            tree.create(self.source, self.output, inventory)
        self.assertFalse(self.output.exists())
        for item in inventory['entries']:
            if item['kind'] == 'unreviewed':
                item['kind'] = 'sqlite' if item['path'] == 'sessions/fixture.db' else 'file'
        tree.create(self.source, self.output, inventory)
        tree.verify(self.output)
        saved = path.read_bytes()
        with self.assertRaises(FileExistsError):
            tree.generate_inventory(self.source, path)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(actual[name], 'unreviewed')

    def test_inventory_rejects_nested_insecure_and_special_sources(self):
        with self.assertRaises(ValueError):
            tree.generate_inventory(self.source, self.source / 'inventory.json')
        self.assertFalse((self.source / 'inventory.json').exists())
        insecure = self.root / 'insecure'
        insecure.mkdir()
        insecure.chmod(0o755)
        with self.assertRaises(ValueError):
            tree.generate_inventory(self.source, insecure / 'inventory.json')
        for mode in ('symlink', 'hardlink', 'fifo', 'wal', 'shm', 'journal'):
            entry = self.source / ('bad-' + mode if mode in ('wal', 'shm', 'journal') else 'bad')
            if mode == 'symlink':
                entry.symlink_to(self.db)
            elif mode == 'hardlink':
                os.link(self.db, entry)
            elif mode == 'fifo':
                os.mkfifo(entry)
            else:
                entry.write_bytes(b'')
            output = self.root / 'inventory.json'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                tree.generate_inventory(self.source, output)
            self.assertFalse(output.exists())
            entry.unlink()

    def test_inventory_never_opens_sqlite_or_reads_file_contents(self):
        path = self.root / 'inventory.json'
        with patch.object(tree.sqlite3, 'connect', side_effect=AssertionError('unexpected sqlite')):
            with patch.object(Path, 'open', side_effect=AssertionError('unexpected file read')):
                tree.generate_inventory(self.source, path)
        self.assertTrue(path.exists())

    def test_inventory_failure_removes_new_file(self):
        original_scan = tree.scan
        calls = []

        def changed(root):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('synthetic failure')
            return original_scan(root)

        path = self.root / 'inventory.json'
        with patch.object(tree, 'scan', side_effect=changed), self.assertRaises(ValueError):
            tree.generate_inventory(self.source, path)
        self.assertFalse(path.exists())

    def test_cli_requires_confirmation(self):
        manifest = self.root / 'inventory.json'
        manifest.write_text(json.dumps(self.inventory))
        command = [sys.executable, str(Path(tree.__file__)), 'create', str(self.source),
                   str(self.output), '--inventory', str(manifest)]
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.output.exists())
        success = subprocess.run(command + ['--offline-confirmed'], capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)


if __name__ == '__main__':
    unittest.main()
