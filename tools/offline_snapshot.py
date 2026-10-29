#!/usr/bin/env python3
"""停止済みデータの限定的な SQLite・ファイルスナップショット。"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing
from pathlib import Path

MANIFEST = "snapshot-manifest.json"
MARKER = ".incomplete"


def fail(message):
    raise ValueError(message)


def checked_path(value):
    if '..' in Path(value).parts:
        fail('親ディレクトリをたどるパスは使用できません')
    path = Path(os.path.abspath(value))
    for part in (path, *path.parents):
        if part.is_symlink():
            fail("シンボリックリンクを含むパスは使用できません")
    return path


def private_dir(path):
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        fail("親ディレクトリは実行ユーザー所有かつ他ユーザーからアクセス不能にしてください")


def safe_name(name):
    if (not isinstance(name, str) or not name or name in {'.', '..', MANIFEST, MARKER}
            or '/' in name or '\x00' in name or name.endswith(('-wal', '-shm', '-journal'))):
        fail("不正または予約済みのファイル名です")
    return name


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        fail("通常ファイル以外またはハードリンクは使用できません")
    return info


def fingerprint(path):
    info = regular(path)
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    after = regular(path)
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        fail("入力ファイルが変化しました")
    return digest.hexdigest()


def check_sidecars(path):
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink():
            fail("SQLite の補助ファイルがシンボリックリンクです")
        if sidecar.exists():
            info = regular(sidecar)
            if suffix in ('-wal', '-journal') and info.st_size:
                fail("非空の SQLite WAL または journal があるため中止しました")


def db_verify(path):
    with path.open('rb') as source:
        if source.read(16) != b'SQLite format 3\x00':
            fail('SQLite ヘッダーが不正です')
    uri = path.as_uri() + '?mode=ro&immutable=1'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            fail("SQLite integrity_check に失敗しました")
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            fail("SQLite foreign_key_check に失敗しました")


def isolated_backup(source, target):
    # 元 DB を SQLite で開くと SHM が変わり得るため、非空 WAL を拒否して私有コピーを開く。
    check_sidecars(source)
    before = fingerprint(source)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        clone = Path(tmp) / 'input.sqlite'
        shutil.copyfile(source, clone)
        os.chmod(clone, 0o600)
        if fingerprint(clone) != before or fingerprint(source) != before:
            fail("入力ファイルが変化しました")
        check_sidecars(source)
        db_verify(clone)
        with closing(sqlite3.connect(clone.as_uri() + '?mode=ro&immutable=1', uri=True)) as old:
            with closing(sqlite3.connect(target)) as new:
                old.backup(new)
        os.chmod(target, 0o600)
        db_verify(target)
        if fingerprint(source) != before:
            fail("入力ファイルが変化しました")
        check_sidecars(source)


def copy_file(source, target):
    before = fingerprint(source)
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
    if fingerprint(target) != before or fingerprint(source) != before:
        fail("入力ファイルが変化しました")


def prepare(source, output):
    source, output = checked_path(source), checked_path(output)
    if not source.is_dir():
        fail("入力ディレクトリがありません")
    if source == output or source in output.parents or output in source.parents:
        fail("入力と出力が同一または入れ子です")
    private_dir(output.parent)
    # mkdir は既存のファイル・ディレクトリ・リンクを置き換えない。
    output.mkdir(mode=0o700)
    return source, output


def create(source, output, databases, files):
    entries = [(safe_name(name), 'sqlite') for name in databases] + [
        (safe_name(name), 'file') for name in files
    ]
    if not entries or len({name for name, _ in entries}) != len(entries):
        fail("入力ファイルを指定し、重複を除いてください")
    source, output = prepare(source, output)
    try:
        (output / MARKER).write_text('incomplete\n', encoding='utf-8')
        manifest = []
        for name, kind in entries:
            src, dst = source / name, output / name
            regular(src)
            if kind == 'sqlite':
                isolated_backup(src, dst)
            else:
                with src.open('rb') as file:
                    if file.read(16) == b'SQLite format 3\x00':
                        fail('SQLite は --sqlite で指定してください')
                copy_file(src, dst)
            manifest.append({'name': name, 'kind': kind, 'sha256': fingerprint(dst)})
        path = output / MANIFEST
        path.write_text(json.dumps({'version': 1, 'files': manifest}, ensure_ascii=True) + '\n',
                        encoding='utf-8')
        os.chmod(path, 0o600)
        verify(output, incomplete=True)
        (output / MARKER).unlink()
    except BaseException:
        shutil.rmtree(output)
        raise


def verify(source, incomplete=False):
    source = checked_path(source)
    private_dir(source)
    manifest_path = source / MANIFEST
    regular(manifest_path)
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'version', 'files'} or data['version'] != 1:
        fail("マニフェストの形式が不正です")
    entries = data['files']
    if not isinstance(entries, list) or not entries:
        fail("マニフェストのファイル一覧が不正です")
    expected = {MANIFEST, MARKER} if incomplete else {MANIFEST}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'name', 'kind', 'sha256'}:
            fail("マニフェスト項目が不正です")
        name = safe_name(entry['name'])
        if name in expected or entry['kind'] not in ('sqlite', 'file'):
            fail("マニフェストの重複または種別が不正です")
        expected.add(name)
        if fingerprint(source / name) != entry['sha256']:
            fail("スナップショットのハッシュが一致しません")
        if entry['kind'] == 'sqlite':
            db_verify(source / name)
    if {p.name for p in source.iterdir()} != expected:
        fail("未登録のファイルまたは未完了の出力が存在します")
    return entries


def restore(source, output):
    source = checked_path(source)
    entries = verify(source)
    source, output = prepare(source, output)
    try:
        (output / MARKER).write_text('incomplete\n', encoding='utf-8')
        for entry in entries:
            src, dst = source / entry['name'], output / entry['name']
            copy_file(src, dst)
            if fingerprint(dst) != entry['sha256']:
                fail("復元元が変化しました")
            if entry['kind'] == 'sqlite':
                db_verify(dst)
        verify(source)
        (output / MARKER).unlink()
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    snap = sub.add_parser('create')
    snap.add_argument('source')
    snap.add_argument('output')
    snap.add_argument('--sqlite', action='append', default=[])
    snap.add_argument('--file', action='append', default=[])
    snap.add_argument('--offline-confirmed', action='store_true', required=True)
    check = sub.add_parser('verify')
    check.add_argument('source')
    back = sub.add_parser('restore')
    back.add_argument('source')
    back.add_argument('output')
    back.add_argument('--offline-confirmed', action='store_true', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'create':
            create(args.source, args.output, args.sqlite, args.file)
        elif args.action == 'verify':
            verify(args.source)
        else:
            restore(args.source, args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # パスやデータ、SQL の内容を出力しない。
        print(f'失敗: {type(exc).__name__}。入力と実行条件を確認してください。', file=sys.stderr)
        return 1
    print('完了')
    return 0


if __name__ == '__main__':
    sys.exit(main())
