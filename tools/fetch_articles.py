"""Resumable full-page acquisition. Run on the user's server."""
import argparse
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db,articles,collector

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--all',action='store_true',help='Усі веборигінали; типово — accepted інфовплив')
    p.add_argument('--retry',action='store_true',help='Повторити також невдалі спроби')
    p.add_argument('--start');p.add_argument('--end');p.add_argument('--delay',type=float,default=1)
    a=p.parse_args();c=db.connect();proxy,_=collector.configure_proxy()
    try:
        sql="SELECT d.id FROM source_documents d WHERE d.source_kind IN ('rss','web') AND d.content_status NOT IN ('extracted','manual')"
        params=[]
        if not a.retry:sql+=" AND d.content_status='pending'"
        if not a.all:sql+=" AND EXISTS (SELECT 1 FROM info_influence i WHERE i.source_document_id=d.id AND i.status='accepted')"
        for op,value in [('>=',a.start),('<',a.end)]:
            if value:
                stamp=collector.datetime.fromisoformat(value)
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=collector.ZoneInfo('Europe/Kyiv'))
                sql+=f' AND d.published_at {op} ?';params.append(stamp.astimezone(collector.timezone.utc).isoformat())
        rows=c.execute(sql+' ORDER BY d.published_at DESC',tuple(params)).fetchall();c.commit()
    finally:c.close()
    for index,row in enumerate(rows,1):
        articles.hydrate(row['id'],proxy)
        if index%25==0 or index==len(rows):print(f'{index}/{len(rows)} перевірено',flush=True)
        time.sleep(max(0,a.delay))
    c=db.connect(readonly=True)
    try:
        for row in c.execute('SELECT content_status,count(*) AS total FROM source_documents GROUP BY content_status'):print(dict(row))
    finally:c.close()

if __name__=='__main__':main()
