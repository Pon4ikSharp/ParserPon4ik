"""Attach a user-supplied complete article when automatic extraction fails."""
import argparse,hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db
p=argparse.ArgumentParser();p.add_argument('id',type=int);p.add_argument('file',type=Path)
p.add_argument('--complete',action='store_true',required=True,help='Підтвердити, що файл містить повний текст статті')
a=p.parse_args();text=a.file.read_text(encoding='utf-8-sig')
if not text.strip():raise ValueError('Порожня стаття')
c=db.connect()
try:
    row=c.execute('SELECT id FROM source_documents WHERE id=? AND source_kind IN (?,?)',(a.id,'rss','web')).fetchone()
    if not row:raise ValueError('Веборигінал із таким ID відсутній')
    digest=hashlib.sha256(text.encode()).hexdigest()
    c.execute("UPDATE source_documents SET full_text=?,source_text=?,content_hash=?,content_status='manual',fetch_error='',fetched_at=now() WHERE id=?",(text,text,digest,a.id))
    c.execute('INSERT INTO original_versions(source_document_id,sha256,source_text,origin) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',(a.id,digest,text,'manual-complete'))
    c.commit();print('Повний текст збережено:',a.id)
finally:c.close()
