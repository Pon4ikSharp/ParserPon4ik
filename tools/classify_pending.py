"""Classify archived originals, using the same primary filter; sends no messages."""
import argparse,asyncio,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db,collector,codex_runtime
from tools.export_originals import timestamp

async def main():
    p=argparse.ArgumentParser();p.add_argument('--start',type=timestamp);p.add_argument('--end',type=timestamp)
    a=p.parse_args();c=db.connect();collector._PROXY_URL,_=collector.configure_proxy();collector.ensure_codex_schema()
    executable=collector.find_codex_executable();lock=asyncio.Lock()
    try:
        sql="""SELECT d.* FROM source_documents d JOIN sources s ON s.id=d.source_id
          WHERE s.for_info AND d.content_status IN ('telegram','extracted','manual')""";params=[]
        for op,value in [('>=',a.start),('<',a.end)]:
            if value:sql+=f' AND d.published_at {op} ?';params.append(value)
        rows=c.execute(sql+' ORDER BY d.published_at,d.id',tuple(params)).fetchall();c.commit()
        checked=0
        for row in rows:
            result=await collector.process_info_source_item(source_text=row['source_text'],source_title=row['source_title'],
                source_url=row['source_url'],publication_date=collector.datetime.fromisoformat(row['published_at']),
                source_chat_id=row['source_chat_id'],source_message_id=row['source_message_id'],source_label='архів',
                source_kind=row['source_kind'],processing_lock=lock,database=c,codex_executable=executable,
                codex_model=codex_runtime.model('collector'))
            c.commit()
            if result[0]=='error':raise RuntimeError('Класифікацію призупинено після помилки. Виправте причину й повторіть команду.')
            checked+=result[0]!='duplicate'
        print('Оброблено нових або змінених оригіналів:',checked)
    finally:c.close()

if __name__=='__main__':asyncio.run(main())
