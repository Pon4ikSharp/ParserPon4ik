"""User-owned source registry, shared by collection and both reports."""
import json
import re
from urllib.parse import urlsplit
from app import db

EXCLUDED = {'insiderukr','voynareal'}

def telegram_name(value):
    value = value.strip().lower().replace('https://','').replace('http://','')
    if value.startswith(('t.me/','telegram.me/')): value=value.split('/',1)[1]
    return value.lstrip('@').split('/')[0].split('?')[0]

def excluded(value): return telegram_name(value) in EXCLUDED

def source_key(url):
    if url.startswith('@') or urlsplit(url).hostname in {'t.me','telegram.me'}:
        return 'telegram:'+telegram_name(url)
    host=(urlsplit(url).hostname or '').lower().removeprefix('www.')
    return 'web:'+host

def endpoints(kind, route):
    if route not in {'br','info'}: raise ValueError(route)
    c=db.connect(readonly=True)
    try:
        extra = (
            " AND lower(e.endpoint) NOT LIKE '%t.me/%'"
            " AND lower(e.endpoint) NOT LIKE '%telegram.me/%'"
            if kind == 'rss' else ''
        )
        rows=c.execute(f'SELECT e.endpoint FROM source_endpoints e JOIN sources s ON s.id=e.source_id '
                       f'WHERE e.kind=? AND e.enabled AND s.enabled AND s.for_{route}{extra} ORDER BY e.endpoint',(kind,)).fetchall()
        return [r['endpoint'] for r in rows if not excluded(r['endpoint'])]
    finally: c.close()

def resolve(c, url='', feed_url='', title=''):
    candidate=(feed_url or url or '').strip()
    row=c.execute('SELECT s.* FROM sources s JOIN source_endpoints e ON s.id=e.source_id WHERE e.endpoint=?',
                  (candidate,)).fetchone()
    if row: return row
    # Telegram post URLs (https://t.me/channel/123) must resolve to the
    # registry endpoint (@channel), otherwise collected posts remain source_id=NULL.
    if candidate.startswith('@') or urlsplit(candidate).hostname in {'t.me','telegram.me'}:
        wanted=telegram_name(candidate)
        rows=c.execute("SELECT s.*,e.endpoint AS _endpoint FROM sources s JOIN source_endpoints e ON s.id=e.source_id WHERE e.kind='telegram' AND e.enabled AND s.enabled").fetchall()
        for item in rows:
            if telegram_name(item['_endpoint'])==wanted:
                return item
    return c.execute('SELECT * FROM sources WHERE source_key=?',(source_key(candidate),)).fetchone()

def audience_for(c, url='',feed_url='',title=''):
    row=resolve(c,url,feed_url,title)
    return list(row['audiences']) if row else []

def attach_document(c, document_id):
    row=c.execute('SELECT * FROM source_documents WHERE id=?',(document_id,)).fetchone()
    source=resolve(c,row['source_url'],row['feed_url'],row['source_title'])
    if source:
        c.execute('UPDATE source_documents SET source_id=? WHERE id=?',(source['id'],document_id))

def ensure_audiences(c, analysis, source_url):
    audiences=audience_for(c,source_url)
    if audiences: analysis['audiences']=audiences
    return analysis


def web_only_endpoints(route):
    if route not in {'br','info'}:
        raise ValueError(route)
    c=db.connect(readonly=True)
    try:
        rows=c.execute(
            f'''SELECT e.endpoint FROM source_endpoints e
                  JOIN sources s ON s.id=e.source_id
                 WHERE e.kind='web' AND e.enabled AND s.enabled AND s.for_{route}
                   AND NOT EXISTS (
                       SELECT 1 FROM source_endpoints x
                        WHERE x.source_id=s.id AND x.enabled
                          AND x.kind IN ('rss','telegram')
                   )
                 ORDER BY e.endpoint'''
        ).fetchall()
        return [r['endpoint'] for r in rows if not excluded(r['endpoint'])]
    finally:
        c.close()
