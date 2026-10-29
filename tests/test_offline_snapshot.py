import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import offline_snapshot as snapshot


class OfflineSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.db = self.source / 'fixture.db'
        with sqlite3.connect(self.db) as con:
            con.execute('create table records (value text)')
            con.execute('insert into records values (?)', ('synthetic',))
        (self.source / 'fixture.json').write_text('{"synthetic":true}', encoding='utf-8')
        self.output = self.root / 'snapshot'

    def create(self):
        snapshot.create(self.source, self.output, ['fixture.db'], ['fixture.json'])

    def test_roundtrip_and_permissions(self):
        before = self.db.read_bytes()
        self.create()
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for item in self.output.iterdir():
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)
        snapshot.verify(self.output)
        restored = self.root / 'restored'
        snapshot.restore(self.output, restored)
        with sqlite3.connect(restored / 'fixture.db') as con:
            self.assertEqual(con.execute('select value from records').fetchall(), [('synthetic',)])
        self.assertEqual((restored / 'fixture.json').read_bytes(),
                         (self.source / 'fixture.json').read_bytes())
        self.assertFalse((restored / snapshot.MARKER).exists())

    def test_sqlite_connections_are_closed(self):
        connect = sqlite3.connect
        connections = []

        def tracked_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with patch.object(snapshot.sqlite3, 'connect', side_effect=tracked_connect):
            self.create()
        self.assertTrue(connections)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('select 1')

    def test_live_wal_is_rejected_without_source_change(self):
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        con.execute('pragma journal_mode=wal')
        con.execute("insert into records values ('wal row')")
        con.commit()
        wal = Path(str(self.db) + '-wal')
        self.assertGreater(wal.stat().st_size, 0)
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir()})

    def test_nonempty_journal_and_symlink_sidecar_rejected(self):
        journal = Path(str(self.db) + '-journal')
        journal.write_bytes(b'journal')
        with self.assertRaises(ValueError):
            self.create()
        journal.unlink()
        journal.symlink_to(self.db)
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_existing_and_nested_output_refused(self):
        self.output.mkdir()
        marker = self.output / 'keep'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(marker.read_text(), 'keep')
        for output in (self.source, self.source / 'child', self.root):
            with self.assertRaises(ValueError):
                snapshot.create(self.source, output, ['fixture.db'], [])

    def test_symlink_hardlink_and_bad_names_refused(self):
        (self.source / 'link').symlink_to(self.db)
        os.link(self.db, self.source / 'hard')
        for name in ('link', 'hard', '../fixture.db', 'fixture.db-wal', snapshot.MANIFEST):
            with self.subTest(name=name), self.assertRaises(ValueError):
                snapshot.create(self.source, self.output, [name], [])
        (self.source / 'hard').unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            snapshot.create(alias, self.output, ['fixture.db'], [])

    def test_tamper_extra_files_and_partial_marker_refused(self):
        self.create()
        path = self.output / 'fixture.json'
        path.write_text('changed')
        with self.assertRaises(ValueError):
            snapshot.restore(self.output, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())
        path.write_text('{"synthetic":true}', encoding='utf-8')
        (self.output / snapshot.MARKER).write_text('incomplete')
        with self.assertRaises(ValueError):
            snapshot.verify(self.output)
        (self.output / snapshot.MARKER).unlink()
        (self.output / 'extra').write_text('unexpected')
        with self.assertRaises(ValueError):
            snapshot.verify(self.output)

    def test_failed_copy_cleans_partial_output(self):
        with patch.object(snapshot, 'copy_file', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse(self.output.exists())

    def test_database_must_be_valid_and_explicitly_typed(self):
        with self.assertRaises(ValueError):
            snapshot.create(self.source, self.output, [], ['fixture.db'])
        (self.source / 'broken.db').write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            snapshot.create(self.source, self.output, ['broken.db'], [])
        self.assertFalse(self.output.exists())

    def test_insecure_parent_refused(self):
        insecure = self.root / 'insecure'
        insecure.mkdir(mode=0o755)
        insecure.chmod(0o755)
        with self.assertRaises(ValueError):
            snapshot.create(self.source, insecure / 'snapshot', ['fixture.db'], [])

    def test_sqlite_integrity_and_foreign_key_fail_closed(self):
        with sqlite3.connect(self.db) as con:
            con.execute('create table parent (id integer primary key)')
            con.execute('create table child (id integer references parent(id))')
            con.execute('insert into child values (42)')
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())
        self.db.write_bytes(b'SQLite format 3\x00' + b'\x00' * 300)
        with self.assertRaises((ValueError, sqlite3.Error)):
            self.create()
        self.assertFalse(self.output.exists())

    def test_symlink_output_and_parent_traversal_refused(self):
        self.output.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.create()
        with self.assertRaises(ValueError):
            snapshot.create(str(self.root / 'alias' / '..' / 'source'),
                            self.root / 'new', ['fixture.db'], [])

    def test_cli_requires_confirmation_and_hides_paths(self):
        tool = Path(snapshot.__file__)
        cmd = [sys.executable, str(tool), 'create', str(self.source), str(self.output),
               '--sqlite', 'fixture.db']
        missing = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        failed = subprocess.run(cmd + ['--offline-confirmed', '--file', 'missing'],
                                capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn(str(self.source), failed.stderr)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
