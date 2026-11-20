import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import db
from app.grok_local_v3 import VERSION
from app.grok_story_sync import sync_story_groups, detach_story_memberships, reconcile_touched_stories

NOVELTY = {
    "new_candidate": "new",
    "duplicate_candidate": "duplicate",
    "development_candidate": "development",
    "unclear": "new",
    "none": "new",
}
MEMBER_RELATION = {
    "duplicate_candidate": "duplicate",
    "development_candidate": "development",
}

def _j(value):
    return json.dumps(value, ensure_ascii=False)

def _validate(batch_path: Path, result_path: Path):
    cmd = [str(ROOT / '.venv' / 'Scripts' / 'python.exe'),
           str(ROOT / 'scripts' / 'grok_local_v3_validate.py'),
           str(batch_path), str(result_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or 'validation failed')
    return json.loads(proc.stdout)

def import_batch(batch_path: Path, result_path: Path, dry_run: bool = False):
    validation = _validate(batch_path, result_path)
    batch = json.loads(batch_path.read_text(encoding='utf-8'))
    result = json.loads(result_path.read_text(encoding='utf-8'))
    run_id = str(batch.get('run_id') or '')
    counts = Counter()
    story_counts = Counter()
    c = db.connect(readonly=False)
    cur = c.raw.cursor()
    try:
        cur.execute('BEGIN')
        doc_ids = [int(row['source_document_id']) for row in result['results']]
        touched_story_ids = detach_story_memberships(cur, doc_ids)
        for row in result['results']:
            doc_id = int(row['source_document_id'])
            decision = str(row['decision'])
            relation = str(row['story_relation'])
            novelty = NOVELTY.get(relation, 'new')
            story_key = str(row.get('matched_story_key') or '').strip()[:180]
            duplicate_id = row.get('duplicate_of_document_id')
            if duplicate_id is not None:
                duplicate_id = int(duplicate_id)
                if not cur.execute('SELECT 1 FROM source_documents WHERE id=%s', (duplicate_id,)).fetchone():
                    raise RuntimeError(f'duplicate target missing for {doc_id}: {duplicate_id}')
            relevant = decision != 'DROP'
            keep = bool(row['keep_for_analysis'])
            cur.execute(
                '''INSERT INTO grok_document_analysis_v1(
                     source_document_id,relevant,keep_for_analysis,priority,
                     speaker,speaker_role,mentioned_actors,main_claim,narratives,
                     target_audiences,influence_methods,likely_objectives,
                     source_role,novelty,story_key,duplicate_of_document_id,
                     added_value,confidence,reason,model,prompt_version,run_id,raw_result,
                     decision,information_trigger,framing,country_spaces,expected_effect,
                     story_relation,insufficient_text,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'[]'::jsonb,%s::jsonb,%s::jsonb,'[]'::jsonb,
                          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                          %s,%s,%s,%s::jsonb,%s,%s,%s,now())
                   ON CONFLICT(source_document_id) DO UPDATE SET
                     relevant=excluded.relevant,keep_for_analysis=excluded.keep_for_analysis,
                     priority=excluded.priority,speaker=excluded.speaker,speaker_role=excluded.speaker_role,
                     mentioned_actors=excluded.mentioned_actors,main_claim=excluded.main_claim,
                     target_audiences=excluded.target_audiences,influence_methods=excluded.influence_methods,
                     source_role=excluded.source_role,novelty=excluded.novelty,story_key=excluded.story_key,
                     duplicate_of_document_id=excluded.duplicate_of_document_id,added_value=excluded.added_value,
                     confidence=excluded.confidence,reason=excluded.reason,model=excluded.model,
                     prompt_version=excluded.prompt_version,run_id=excluded.run_id,raw_result=excluded.raw_result,
                     decision=excluded.decision,information_trigger=excluded.information_trigger,
                     framing=excluded.framing,country_spaces=excluded.country_spaces,
                     expected_effect=excluded.expected_effect,story_relation=excluded.story_relation,
                     insufficient_text=excluded.insufficient_text,updated_at=now()''',
                (doc_id,relevant,keep,str(row['priority']),str(row['speaker'])[:300],str(row['speaker_role']),
                 _j(row['mentioned_actors']),str(row['main_claim'])[:4000],_j(row['target_audiences']),
                 _j(row['influence_methods']),str(row['source_role']),novelty,story_key,duplicate_id,
                 str(row['added_value'])[:4000],int(row['confidence']),str(row['reason'])[:5000],
                 'supergrok-local',VERSION,run_id,_j(row),decision,str(row['information_trigger'])[:1000],
                 str(row['framing'])[:4000],_j(row['country_spaces']),str(row['expected_effect'])[:3000],
                 relation,bool(row['insufficient_text']))
            )
            cur.execute("INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload) VALUES(%s,'local_v3_import',%s::jsonb)",
                        (doc_id, _j({'run_id':run_id,'version':VERSION,'result':row})))
            counts[decision] += 1
        story_summary = sync_story_groups(cur, batch, result)
        story_counts.update(story_summary)
        story_counts.update(reconcile_touched_stories(cur, touched_story_ids))
        cur.execute('ROLLBACK' if dry_run else 'COMMIT')
    except Exception:
        cur.execute('ROLLBACK')
        raise
    finally:
        c.close()
    return {'ok':True,'dry_run':dry_run,'run_id':run_id,'processed':sum(counts.values()),
            'decisions':dict(counts),'stories':dict(story_counts),'validation':validation}

def main():
    p=argparse.ArgumentParser()
    p.add_argument('batch')
    p.add_argument('result')
    p.add_argument('--dry-run', action='store_true')
    args=p.parse_args()
    print(json.dumps(import_batch(Path(args.batch),Path(args.result),dry_run=args.dry_run),ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
