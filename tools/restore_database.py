"""Restore the verified snapshot into an EMPTY PostgreSQL database."""
import gzip
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import connect,ROOT

def restore(path):
    c=connect()
    try:
        if c.raw.execute("SELECT to_regclass('public.source_documents')").fetchone()[0]:
            raise RuntimeError('Цільова база вже містить source_documents. Відновлення дозволене лише в порожню БД.')
        c.commit()
        with gzip.open(path,'rt',encoding='utf-8') as src:
            statements=[]
            for line in src:
                if line.startswith('COPY public.'):
                    if statements:c.raw.execute(''.join(statements));statements=[]
                    with c.raw.cursor().copy(line.strip().rstrip(';')) as copy:
                        for record in src:
                            if record=='\\.\n':break
                            copy.write(record)
                else:statements.append(line)
            if statements:c.raw.execute(''.join(statements))
        # The snapshot contains the migrated corpus; extensions keep a fresh
        # install aligned with later idempotent upgrades.
        c.raw.execute((ROOT/'database/extensions.sql').read_text(encoding='utf-8'))
        c.raw.execute((ROOT/'database/post_import.sql').read_text(encoding='utf-8'))
        c.commit()
        print('PostgreSQL: об’єднану базу відновлено.')
        for table in ['sources','source_documents','info_influence','news']:
            print(table,c.raw.execute('SELECT count(*) FROM '+table).fetchone()[0])
    except BaseException:c.rollback();raise
    finally:c.close()

if __name__=='__main__':restore(Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'database/merged.sql.gz')
