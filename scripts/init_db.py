#!/usr/bin/env python3
"""Initialize the SQLite database schema."""

from __future__ import annotations

import argparse
from pathlib import Path

from glp1_common import DEFAULT_DB, connect_db, ensure_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = connect_db(args.db)
    ensure_schema(conn)
    conn.close()
    print(f"initialized {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
