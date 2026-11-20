import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.grok_local_v3 import prepare_run


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Grok Local v3 file-route run")
    parser.add_argument("--target", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=0, help="0=adaptive 120-250 per context by text volume; run target defaults to 500")
    args = parser.parse_args()
    manifest = prepare_run(target=args.target, batch_size=args.batch_size)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
