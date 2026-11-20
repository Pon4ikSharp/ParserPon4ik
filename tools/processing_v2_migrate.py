"""Install the additive Processing Engine v2 schema.

Safe default: without --apply this command only reports whether the v2 tables
already exist. It never seeds jobs and never starts workers.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse

from app.db import connect
from app import processing_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create processing_v2_* tables and indexes. Does not seed or run jobs.",
    )
    args = parser.parse_args()

    connection = connect()
    try:
        exists = connection.execute(
            "SELECT to_regclass('public.processing_v2_jobs') AS name"
        ).fetchone()["name"]

        if not args.apply:
            print("processing_v2_jobs:", "installed" if exists else "not installed")
            print("No changes made. Use --apply to install the additive schema.")
            return 0

        if exists:
            print("Processing Engine v2 schema already exists; applying idempotent DDL.")
        processing_v2.install_schema(connection)
        print("Processing Engine v2 schema installed.")
        print("No jobs were seeded and no workers were started.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
