"""Read-only original export for manual analysis, without text truncation."""
import argparse
import json
import sys
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db

def timestamp(value):
    dt=datetime.fromisoformat(value)
    if dt.tzinfo is None:dt=dt.replace(tzinfo=ZoneInfo('Europe/Kyiv'))
    return dt.astimezone(timezone.utc).isoformat()

def main():
    p=argparse.ArgumentParser();p.add_argument('--start',required=True,type=timestamp);p.add_argument('--end',required=True,type=timestamp)
    p.add_argument('--query',default='');p.add_argument('--group');p.add_argument('--info-only',action='store_true')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    sql='SELECT * FROM originals_search WHERE published_at>=? AND published_at<?';params=[a.start,a.end]
    if a.query:sql+=" AND to_tsvector('simple',source_text) @@ plainto_tsquery('simple',?)";params.append(a.query)
    if a.group:sql+=' AND groups @> ?::jsonb';params.append(json.dumps([a.group],ensure_ascii=False))
    if a.info_only:sql+=" AND influence_status='accepted'"
    c=db.connect(readonly=True)
    try:
        rows=c.execute(sql+' ORDER BY published_at,id',tuple(params)).fetchall()
        a.output.mkdir(parents=True,exist_ok=True)
        with (a.output/'originals.jsonl').open('w',encoding='utf-8') as out:
            for row in rows:
                out.write(json.dumps(dict(row),ensure_ascii=False,default=str)+'\n')
                text=f"{row['source_title']}\n{row['published_at']}\n{row['source_url']}\nСтатус: {row['content_status']}\n\n{row['source_text']}"
                (a.output/f"{row['id']}.txt").write_text(text,encoding='utf-8')
        print(f'Експортовано {len(rows)} оригіналів повністю.')
    finally:c.close()

if __name__=='__main__':main()
