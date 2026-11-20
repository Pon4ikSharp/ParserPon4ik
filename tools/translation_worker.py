from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, translation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    connection = db.connect(readonly=False)
    try:
        while True:
            task = translation.claim(connection)
            if not task:
                return
            try:
                translation.process_one(connection, task)
                print(f"translated {task['source_document_id']} via {translation.PROVIDER}", flush=True)
            except Exception as error:
                translation.fail(connection, task, error)
                print(f"translation failed {task['source_document_id']}: {error}", flush=True)
            if not args.drain:
                return
    finally:
        connection.close()


if __name__ == "__main__":
    main()

