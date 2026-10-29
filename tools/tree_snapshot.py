#!/usr/bin/env python3
"""明示台帳と完全一致する停止済みツリーの限定スナップショット。"""
import argparse
import json
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

if __package__:
    from .offline_snapshot import (
        MARKER,
        checked_path,
        db_verify,
        fail,
        fingerprint,
        isolated_backup,
        private_dir,
        regular,
    )
else:
    from offline_snapshot import (
        MARKER,
        checked_path,
        db_verify,
        fail,
        fingerprint,
        isolated_backup,
        private_dir,
        regular,
    )

MANIFEST = 'tree-manifest.json'
DATA = 'data'
KINDS = {'dir', 'file', 'sqlite'}


def parse_entries(data, with_hash=False):
    if not isinstance(data, dict) or set(data) != {'version', 'entries'} or data['version'] != 1:
        fail('台帳の形式が不正です')
    if not isinstance(data['entries'], list):
        fail('台帳の一覧が不正です')
    entries = {}
    for item in data['entries']:
        if not isinstance(item, dict):
            fail('台帳の項目が不正です')
        name, kind = item.get('path'), item.get('kind')
        keys = {'path', 'kind', 'sha256'} if with_hash and kind != 'dir' else {'path', 'kind'}
        if set(item) != keys or not isinstance(name, str) or not isinstance(kind, str):
            fail('台帳の項目が不正です')
        parts = name.split('/')
        if (kind not in KINDS or any(p in ('', '.', '..') for p in parts)
                or '\x00' in name or chr(92) in name or name == MARKER or name in entries):
            fail('台帳のパスまたは種別が不正です')
        if kind != 'dir' and name.endswith(('-wal', '-shm', '-journal')):
            fail('SQLite 補助ファイルを含めることはできません')
        if with_hash and kind != 'dir':
            digest = item['sha256']
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)):
                fail('ハッシュが不正です')
        entries[name] = item
    for name in entries:
        parts = name.split('/')
        for i in range(1, len(parts)):
            parent = '/'.join(parts[:i])
            if parent not in entries or entries[parent]['kind'] != 'dir':
                fail('親ディレクトリが台帳にありません')
    return entries


def scan(root):
    root = checked_path(root)
    if not root.is_dir():
        fail('ディレクトリがありません')
    found = {}
    stack = [(root, '')]
    while stack:
        folder, prefix = stack.pop()
        with os.scandir(folder) as children:
            for child in children:
                name = prefix + child.name
                st = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    found[name] = 'dir'
                    stack.append((Path(child.path), name + '/'))
                elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                    if name.endswith(('-wal', '-shm', '-journal')):
                        fail('SQLite 補助ファイルが存在します')
                    found[name] = 'file'
                else:
                    fail('リンクまたは特殊ファイルが存在します')
    return found


def compare_tree(root, entries, incomplete=False):
    actual = scan(root)
    if incomplete and actual.pop(MARKER, None) != 'file':
        fail('未完了マーカーがありません')
    expected = {path: 'dir' if item['kind'] == 'dir' else 'file'
                for path, item in entries.items()}
    if actual != expected:
        fail('ツリーと台帳が一致しません')


def hashes(root, entries):
    return {name: fingerprint(root / name) for name, item in entries.items()
            if item['kind'] != 'dir'}


def read_json(path):
    path = checked_path(path)
    regular(path)
    return json.loads(path.read_text(encoding='utf-8'))


def generate_inventory(source, output):
    source, output = checked_path(source), checked_path(output)
    if source == output or source in output.parents or output in source.parents:
        fail('台帳は入力ツリー外に作成してください')
    private_dir(output.parent)
    if stat.S_IMODE(output.parent.stat().st_mode) != 0o700:
        fail('台帳の親ディレクトリは 0700 にしてください')
    initial = scan(source)
    entries = [{'path': name, 'kind': kind} for name, kind in sorted(initial.items())]
    parse_entries({'version': 1, 'entries': entries})
    for item in entries:
        if item['kind'] == 'file':
            item['kind'] = 'unreviewed'
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({'version': 1, 'entries': entries}, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
        if scan(source) != initial:
            fail('走査中に入力ツリーが変化しました')
    except BaseException:
        output.unlink()
        raise


def create(source, output, inventory):
    entries = parse_entries(inventory)
    source, output = checked_path(source), checked_path(output)
    if source == output or source in output.parents or output in source.parents:
        fail('入力と出力が同一または入れ子です')
    private_dir(output.parent)
    compare_tree(source, entries)
    before = hashes(source, entries)
    compare_tree(source, entries)
    output.mkdir(mode=0o700)
    try:
        (output / MARKER).write_text('incomplete\n', encoding='utf-8')
        data = output / DATA
        data.mkdir(mode=0o700)
        for name, item in sorted(entries.items(), key=lambda pair: (pair[0].count('/'), pair[0])):
            src, dst = source / name, data / name
            if item['kind'] == 'dir':
                dst.mkdir(mode=0o700)
            elif item['kind'] == 'sqlite':
                regular(src)
                isolated_backup(src, dst)
            else:
                regular(src)
                with src.open('rb') as file:
                    if file.read(16) == b'SQLite format 3\x00':
                        fail('SQLite を通常ファイルとして扱うことはできません')
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
                if fingerprint(dst) != before[name]:
                    fail('ファイルが変化しました')
        compare_tree(source, entries)
        if hashes(source, entries) != before:
            fail('入力ツリーが変化しました')
        compare_tree(source, entries)
        records = []
        for name, item in entries.items():
            record = {'path': name, 'kind': item['kind']}
            if item['kind'] != 'dir':
                record['sha256'] = fingerprint(data / name)
            records.append(record)
        manifest = output / MANIFEST
        manifest.write_text(
            json.dumps({'version': 1, 'entries': records}, ensure_ascii=True) + '\n',
            encoding='utf-8',
        )
        os.chmod(manifest, 0o600)
        verify(output, incomplete=True)
        (output / MARKER).unlink()
    except BaseException:
        shutil.rmtree(output)
        raise


def verify(source, incomplete=False):
    source = checked_path(source)
    private_dir(source)
    expected = {MANIFEST, DATA, MARKER} if incomplete else {MANIFEST, DATA}
    if {p.name for p in source.iterdir()} != expected:
        fail('余分なファイルまたは未完了マーカーが存在します')
    data = source / DATA
    if data.is_symlink():
        fail('シンボリックリンクは使用できません')
    private_dir(data)
    entries = parse_entries(read_json(source / MANIFEST), with_hash=True)
    compare_tree(data, entries)
    for name, item in entries.items():
        if item['kind'] != 'dir':
            if fingerprint(data / name) != item['sha256']:
                fail('ハッシュが一致しません')
            if item['kind'] == 'sqlite':
                db_verify(data / name)
    compare_tree(data, entries)
    return entries


def restore(source, output):
    source, output = checked_path(source), checked_path(output)
    if source == output or source in output.parents or output in source.parents:
        fail('入力と出力が同一または入れ子です')
    entries = verify(source)
    private_dir(output.parent)
    output.mkdir(mode=0o700)
    try:
        (output / MARKER).write_text('incomplete\n', encoding='utf-8')
        for name, item in sorted(entries.items(), key=lambda pair: (pair[0].count('/'), pair[0])):
            dst = output / name
            if item['kind'] == 'dir':
                dst.mkdir(mode=0o700)
            else:
                src = source / DATA / name
                regular(src)
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
                if fingerprint(dst) != item['sha256']:
                    fail('復元中にファイルが変化しました')
                if item['kind'] == 'sqlite':
                    db_verify(dst)
        verify(source)
        compare_tree(output, entries, incomplete=True)
        if any(fingerprint(output / name) != item['sha256'] for name, item in entries.items()
               if item['kind'] != 'dir'):
            fail('復元結果が一致しません')
        (output / MARKER).unlink()
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    inventory = sub.add_parser('inventory')
    inventory.add_argument('source')
    inventory.add_argument('output')
    inventory.add_argument('--offline-confirmed', action='store_true', required=True)
    new = sub.add_parser('create')
    new.add_argument('source')
    new.add_argument('output')
    new.add_argument('--inventory', required=True)
    new.add_argument('--offline-confirmed', action='store_true', required=True)
    check = sub.add_parser('verify')
    check.add_argument('source')
    old = sub.add_parser('restore')
    old.add_argument('source')
    old.add_argument('output')
    old.add_argument('--offline-confirmed', action='store_true', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'inventory':
            generate_inventory(args.source, args.output)
        elif args.action == 'create':
            source = checked_path(args.source)
            output = checked_path(args.output)
            inventory = checked_path(args.inventory)
            if source in inventory.parents or output in inventory.parents:
                fail('台帳は入力・出力ツリー外に置いてください')
            create(source, output, read_json(inventory))
        elif args.action == 'verify':
            verify(args.source)
        else:
            restore(args.source, args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'失敗: {type(exc).__name__}。入力と実行条件を確認してください。', file=sys.stderr)
        return 1
    print('完了')
    return 0


if __name__ == '__main__':
    sys.exit(main())
