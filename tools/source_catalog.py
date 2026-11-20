"""Export current routes/audiences, including missing subscriptions, as JSON."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('sources_catalog.json'));a=p.parse_args()
c=db.connect(readonly=True)
try:
    rows=c.execute('SELECT * FROM sources ORDER BY source_key').fetchall()
    for row in rows:row['endpoints']=[dict(e) for e in c.execute('SELECT endpoint,kind,enabled FROM source_endpoints WHERE source_id=?',(row['id'],))]
    a.output.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8');print('Джерел:',len(rows))
finally:c.close()
