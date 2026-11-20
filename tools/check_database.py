import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db

c=db.connect(readonly=True)
try:
    for name in ['sources','source_endpoints','source_documents','info_influence','news','original_versions',
                 'note_drafts','event_candidates','calendar_events','analysis_jobs']:
        print(name,c.execute('SELECT count(*) FROM '+name).fetchone()[0])
    orphan=c.execute('SELECT count(*) FROM info_influence i LEFT JOIN source_documents d ON d.id=i.source_document_id WHERE d.id IS NULL').fetchone()[0]
    removed=c.execute("SELECT count(*) FROM source_documents WHERE lower(source_url) LIKE '%t.me/insiderukr/%' OR lower(source_url) LIKE '%t.me/voynareal/%'").fetchone()[0]
    print('Непов’язаних індикаторів:',orphan,'Оригіналів виключених каналів:',removed)
    if orphan or removed:raise RuntimeError('Перевірка цілісності не пройдена')
    for row in c.execute('SELECT content_status,count(*) AS total FROM source_documents GROUP BY content_status'):print(dict(row))
finally:c.close()
