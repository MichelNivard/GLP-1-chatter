#!/usr/bin/env python3
"""Pack and unpack the SQLite database as small Git-friendly artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from glp1_common import DEFAULT_DB, ROOT

DEFAULT_ARTIFACT_DIR = ROOT / "data" / "db-artifact"
DEFAULT_CHUNK_SIZE_MB = 75
PART_PREFIX = "glp1_reports.sqlite3.gz.part"
MANIFEST_NAME = "manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack = subparsers.add_parser("pack", help="Compact, gzip, and split the SQLite database")
    pack.add_argument("--db", type=Path, default=DEFAULT_DB)
    pack.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    pack.add_argument("--chunk-size-mb", type=int, default=DEFAULT_CHUNK_SIZE_MB)

    unpack = subparsers.add_parser("unpack", help="Restore the SQLite database from packed parts")
    unpack.add_argument("--db", type=Path, default=DEFAULT_DB)
    unpack.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    unpack.add_argument("--if-present", action="store_true", help="Succeed when no artifact exists")
    unpack.add_argument("--force", action="store_true", help="Overwrite an existing database")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quote_sql_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def compact_database(db_path: Path, compacted_path: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    if compacted_path.exists():
        compacted_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute(f"VACUUM INTO {quote_sql_path(compacted_path)}")
    finally:
        conn.close()


def gzip_file(source: Path, destination: Path) -> None:
    if destination.exists():
        destination.unlink()
    with source.open("rb") as src, destination.open("wb") as raw_dst:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_dst, compresslevel=9, mtime=0) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)


def split_file(source: Path, artifact_dir: Path, chunk_size: int) -> list[dict[str, object]]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for old_part in artifact_dir.glob(f"{PART_PREFIX}*"):
        old_part.unlink()
    manifest = artifact_dir / MANIFEST_NAME
    if manifest.exists():
        manifest.unlink()

    parts: list[dict[str, object]] = []
    with source.open("rb") as handle:
        index = 1
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            part_name = f"{PART_PREFIX}{index:04d}"
            part_path = artifact_dir / part_name
            part_path.write_bytes(chunk)
            parts.append(
                {
                    "name": part_name,
                    "size": len(chunk),
                    "sha256": hashlib.sha256(chunk).hexdigest(),
                }
            )
            index += 1
    return parts


def pack(db_path: Path, artifact_dir: Path, chunk_size_mb: int) -> None:
    chunk_size = chunk_size_mb * 1024 * 1024
    if chunk_size <= 0:
        raise ValueError("--chunk-size-mb must be positive")

    with tempfile.TemporaryDirectory(prefix="glp1-db-pack-") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        compacted = tmp_dir / "glp1_reports.sqlite3"
        compressed = tmp_dir / "glp1_reports.sqlite3.gz"
        compact_database(db_path, compacted)
        gzip_file(compacted, compressed)
        parts = split_file(compressed, artifact_dir, chunk_size)
        manifest = {
            "format": 1,
            "database": db_path.name,
            "chunk_size": chunk_size,
            "uncompressed_size": compacted.stat().st_size,
            "uncompressed_sha256": sha256_file(compacted),
            "compressed_size": compressed.stat().st_size,
            "compressed_sha256": sha256_file(compressed),
            "parts": parts,
        }
        (artifact_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact_dir": str(artifact_dir),
                "compressed_size": manifest["compressed_size"],
                "parts": len(parts),
                "uncompressed_size": manifest["uncompressed_size"],
            },
            sort_keys=True,
        )
    )


def read_manifest(artifact_dir: Path, if_present: bool) -> dict[str, object] | None:
    manifest_path = artifact_dir / MANIFEST_NAME
    if not manifest_path.exists():
        if if_present:
            print(f"no database artifact found at {manifest_path}")
            return None
        raise FileNotFoundError(f"database artifact manifest does not exist: {manifest_path}")
    return json.loads(manifest_path.read_text())


def unpack(db_path: Path, artifact_dir: Path, if_present: bool, force: bool) -> None:
    manifest = read_manifest(artifact_dir, if_present)
    if manifest is None:
        return
    if db_path.exists() and not force:
        print(f"database already exists: {db_path}")
        return

    with tempfile.TemporaryDirectory(prefix="glp1-db-unpack-") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        compressed = tmp_dir / "glp1_reports.sqlite3.gz"
        with compressed.open("wb") as output:
            for part in manifest["parts"]:
                part_path = artifact_dir / str(part["name"])
                chunk = part_path.read_bytes()
                expected_sha = str(part["sha256"])
                if hashlib.sha256(chunk).hexdigest() != expected_sha:
                    raise ValueError(f"sha256 mismatch for {part_path}")
                output.write(chunk)
        if sha256_file(compressed) != str(manifest["compressed_sha256"]):
            raise ValueError("combined compressed artifact sha256 mismatch")

        restored = tmp_dir / "glp1_reports.sqlite3"
        with gzip.open(compressed, "rb") as src, restored.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        if sha256_file(restored) != str(manifest["uncompressed_sha256"]):
            raise ValueError("restored database sha256 mismatch")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(restored), db_path)
    print(f"restored {db_path}")


def main() -> int:
    args = parse_args()
    if args.command == "pack":
        pack(args.db, args.artifact_dir, args.chunk_size_mb)
    elif args.command == "unpack":
        unpack(args.db, args.artifact_dir, args.if_present, args.force)
    else:  # pragma: no cover
        raise ValueError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
