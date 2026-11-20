from __future__ import annotations

import json, sys, uuid, subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db
from app import grok_local_v3 as g

BASE = ROOT / 'runtime' / 'grok_local_v3'


def blocking_run():
    validator = ROOT / 'scripts' / 'grok_local_v3_validate.py'
    py = ROOT / '.venv' / 'Scripts' / 'python.exe'
    for mp in sorted(BASE.glob('*/manifest.json')):
        try:
            m = json.loads(mp.read_text(encoding='utf-8'))
            if not m.get('catchup_mode') or m.get('catchup_release'):
                continue
            for bp in m.get('batch_paths') or []:
                b = json.loads(Path(bp).read_text(encoding='utf-8'))
                rp = Path(b['result_path'])
                if not rp.exists() or rp.stat().st_size < 20:
                    return {'status':'waiting_previous','run_id':m.get('run_id'),'result_path':str(rp)}
                proc = subprocess.run([str(py), str(validator), str(bp), str(rp)], capture_output=True, text=True, encoding='utf-8')
                if proc.returncode:
                    return {'status':'invalid_previous','run_id':m.get('run_id'),'result_path':str(rp),'error':(proc.stderr or proc.stdout)[-1200:]}
        except Exception as e:
            return {'status':'broken_previous','manifest':str(mp),'error':str(e)}
    return None


def pending_ids() -> set[int]:
    ids: set[int] = set()
    if not BASE.exists():
        return ids
    for mp in BASE.glob('*/manifest.json'):
        try:
            m = json.loads(mp.read_text(encoding='utf-8'))
            if not m.get('catchup_mode') or m.get('catchup_release'):
                continue
            for bp in m.get('batch_paths') or []:
                b = json.loads(Path(bp).read_text(encoding='utf-8'))
                ids.update(int(x['source_document_id']) for x in b.get('items') or [])
        except Exception:
            continue
    return ids


def select_unreserved(conn, target: int, reserved: set[int]):
    counts = g.backlog_counts(conn)
    budgets = g._dynamic_route_targets(counts, target)
    selected = []
    used = set(reserved)
    for route in g.ROUTES:
        budget = int(budgets.get(route, 0))
        if budget <= 0:
            continue
        rows = g._eligible_rows(conn, route, budget + len(reserved) + 50, oldest=False)
        for row in rows:
            did = int(row['id'])
            if did in used:
                continue
            selected.append(row)
            used.add(did)
            if sum(1 for x in selected if x['route'] == route) >= budget:
                break
    if len(selected) < target:
        for route in g.ROUTES:
            rows = g._eligible_rows(conn, route, target + len(reserved) + 100, oldest=True)
            for row in rows:
                did = int(row['id'])
                if did in used:
                    continue
                selected.append(row)
                used.add(did)
                if len(selected) >= target:
                    break
            if len(selected) >= target:
                break
    rank = {'MUST_ANALYZE': 0, 'ANALYZE': 1}
    selected.sort(key=lambda r: (rank.get(str(r.get('route')), 9), r['ts'], int(r['id'])))
    return selected[:target], counts
def create_run(target: int = 200):
    target = max(1, min(int(target), 200))
    blocker = blocking_run()
    if blocker:
        return {'ok': False, **blocker}
    reserved = pending_ids()
    conn = db.connect(readonly=True)
    try:
        selected, counts = select_unreserved(conn, target, reserved)
        stories = g._story_index(conn)
        items = [g._document_context(conn, row, stories) for row in selected]
    finally:
        conn.close()
    if not items:
        return {'ok': True, 'status': 'empty', 'reserved': len(reserved), 'backlog_db': counts}
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
    run_dir = BASE / run_id
    batch_dir = run_dir / 'batches'
    result_dir = run_dir / 'results'
    batch_dir.mkdir(parents=True, exist_ok=False)
    result_dir.mkdir(parents=True, exist_ok=True)
    batch_id = f'{run_id}_b01'
    result_path = result_dir / f'{batch_id}.result.json'
    batch_path = batch_dir / f'{batch_id}.json'
    payload = {'batch_id': batch_id, 'analyzer_version': g.VERSION, 'run_id': run_id,
               'batch_index': 1, 'result_path': str(result_path),
               'system_prompt_path': str(ROOT/'grok'/'local_v3'/'SYSTEM_PROMPT_V3.md'),
               'schema_path': str(ROOT/'grok'/'local_v3'/'OUTPUT_SCHEMA_V3.json'),
               'item_count': len(items), 'items': items}
    batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    manifest = {'run_id': run_id, 'analyzer_version': g.VERSION,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'target': target, 'selected': len(items), 'batch_count': 1,
                'batch_item_counts': [len(items)],
                'route_counts': dict(Counter(i['route'] for i in items)),
                'backlog_before': counts, 'reserved_pending_before': len(reserved),
                'batch_paths': [str(batch_path)], 'results_dir': str(result_dir),
                'auto_import': False, 'catchup_mode': True,
                'scope_start': g.SCOPE_START}
    manifest_path = run_dir / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    start = g._start_instructions(manifest_path, [str(batch_path)])
    start += '\nCATCH-UP MODE:\n- DO NOT import to PostgreSQL.\n- After validator OK, immediately run grok_catchup_prepare.py again for next 200.\n- If it returns status=empty, stop: current reserved+imported queue is exhausted.\n'
    (run_dir/'GROK_START.txt').write_text(start, encoding='utf-8')
    return {'ok': True, 'status': 'prepared', 'run_id': run_id,
            'batch_id': batch_id, 'selected': len(items),
            'route_counts': manifest['route_counts'], 'result_path': str(result_path),
            'batch_path': str(batch_path), 'reserved_pending_before': len(reserved)}


if __name__ == '__main__':
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    print(json.dumps(create_run(target), ensure_ascii=False, indent=2))
