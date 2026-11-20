import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.grok_local_v3_import import import_batch
from app.grok_local_v3 import VERSION

BASE = ROOT / 'runtime' / 'grok_local_v3'
LOG = ROOT / 'runtime' / 'logs' / 'grok_local_v3_watch.log'

def log(payload):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {'at': datetime.now(timezone.utc).isoformat(), **payload}
    with LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')

def process_once():
    stats = {'runs':0,'skipped_legacy':0,'batches_seen':0,'imported':0,'waiting':0,'errors':0}
    if not BASE.exists():
        return stats
    for manifest_path in sorted(BASE.glob('*/manifest.json')):
        stats['runs'] += 1
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception as e:
            stats['errors'] += 1; log({'status':'manifest_error','path':str(manifest_path),'error':str(e)[:500]}); continue
        if str(manifest.get('analyzer_version') or '') != VERSION:
            stats['skipped_legacy'] += 1
            continue
        if manifest.get('auto_import', True) is False:
            continue
        run_imported = 0
        for raw_batch in manifest.get('batch_paths') or []:
            stats['batches_seen'] += 1
            batch_path = Path(raw_batch)
            try:
                batch = json.loads(batch_path.read_text(encoding='utf-8'))
                result_path = Path(batch['result_path'])
                marker = result_path.with_suffix(result_path.suffix + '.imported.json')
                if marker.exists():
                    continue
                if not result_path.exists() or result_path.stat().st_size < 20:
                    stats['waiting'] += 1; continue
                summary = import_batch(batch_path, result_path, dry_run=False)
                marker.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                stats['imported'] += 1; run_imported += 1
                log({'status':'imported','run_id':manifest.get('run_id'),'batch':batch.get('batch_id'),'summary':summary})
            except Exception as e:
                stats['errors'] += 1
                log({'status':'batch_error','run_id':manifest.get('run_id'),'batch_path':str(raw_batch),'error':str(e)[:1000]})
        if run_imported:
            marker = manifest_path.parent / 'import_status.json'
            marker.write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'run_id':manifest.get('run_id'),'imported_now':run_imported}, ensure_ascii=False, indent=2), encoding='utf-8')
    return stats

def main():
    print(json.dumps(process_once(), ensure_ascii=False))

if __name__ == '__main__':
    main()
