#!/usr/bin/env python3
"""Git 追跡ファイルに秘密情報や実行データが混入していないか検査する。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

BLOCKED_DIRECTORIES = {
    "data",
    "exports",
    "html-pending",
    "join_voice",
    "logs",
    "media",
    "miq_emojis",
    "miq_fonts",
    "runs",
    "runtime",
    "secrets",
    "sounds",
    "wav_rank",
}
BLOCKED_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".flac",
    ".gguf",
    ".gif",
    ".jsonl",
    ".key",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".otf",
    ".p12",
    ".pcm",
    ".pem",
    ".pfx",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".token",
    ".ttf",
    ".wav",
    ".webm",
    ".woff",
    ".woff2",
}
SECRET_MARKERS = tuple(
    f"-----BEGIN {label}PRIVATE KEY-----" for label in ("OPENSSH ", "RSA ", "EC ", "")
)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode() for item in result.stdout.split(b"\0") if item]


def path_problem(name: str) -> str | None:
    path = PurePosixPath(name)
    if name != ".env.example" and (name == ".env" or name.startswith(".env.")):
        return "実環境の env file"
    if any(part in BLOCKED_DIRECTORIES for part in path.parts):
        return "実行データ用 directory"
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        return "音声・モデル・秘密情報などの除外対象 file"
    return None


def main() -> int:
    files = tracked_files()
    problems: list[str] = []
    for name in files:
        if problem := path_problem(name):
            problems.append(f"{name}: {problem}")
            continue

        path = Path(name)
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        payload = path.read_bytes()
        if b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="replace")
        for marker in SECRET_MARKERS:
            if marker in text:
                problems.append(f"{name}: private key marker")
                break

    if problems:
        print("Git へ含められないファイルを検出しました:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print(f"リポジトリ検査 OK（{len(files)} files）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
