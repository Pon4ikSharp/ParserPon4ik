"""Capture original web pages without bypassing paywalls or access controls."""
import hashlib
import ipaddress
import json
import socket
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urljoin
import httpx
import trafilatura
from bs4 import BeautifulSoup
from app import db

def public_url(url):
    p=urlsplit(url)
    if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password:
        raise ValueError('Unsupported article URL')
    for addr in socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80)):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise ValueError('Article URL resolves to a private address')
    return url

def extract_html(html, url, feed_text=''):
    soup=BeautifulSoup(html,'html.parser')
    body=trafilatura.extract(html,url=url,include_comments=False,include_tables=True,
                            favor_recall=True,deduplicate=False) or ''
    title=(soup.title.get_text(' ',strip=True) if soup.title else '')
    tags=[x.get('content','') for x in soup.select('meta[property="article:tag"],meta[name="keywords"]')]
    quotes=[x.get_text(' ',strip=True) for x in soup.select('blockquote')]
    metadata={'title':title,'tags':tags,'hashtags':list(dict.fromkeys(re.findall(r'#[\w]+',body))),
              'blockquotes':quotes,'url':url,'extractor':'trafilatura'}
    paywall=bool(soup.select('[class*="paywall"], [id*="paywall"]'))
    structured=' '.join(x.get_text() for x in soup.select('script[type="application/ld+json"]'))
    paywall=paywall or bool(re.search(r'"isAccessibleForFree"\s*:\s*(?:false|"false")',structured,re.I))
    blocked=any(x in (title+' '+body[:500]).lower() for x in ['just a moment','access denied','captcha','доступ ограничен'])
    # Conservative: retained extraction is not a guarantee of publisher completeness.
    status='extracted' if len(body)>=300 else 'partial'
    if paywall: status='paywall'
    if blocked: status='blocked'
    return body,status,metadata

def fetch(url, feed_text='', proxy=None, timeout=40):
    current=url
    with httpx.Client(timeout=timeout,proxy=proxy,trust_env=False,follow_redirects=False,
                      headers={'User-Agent':'Mozilla/5.0 (compatible; Pon4ikArchive/3.0)'}) as client:
        for _ in range(6):
            public_url(current)
            with client.stream('GET',current) as response:
                if response.is_redirect:
                    current=urljoin(current,response.headers['location']);continue
                response.raise_for_status()
                if 'html' not in response.headers.get('content-type','').lower():
                    raise ValueError('Article response is not HTML')
                chunks=[];size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>20_000_000: raise ValueError('Article page exceeds 20 MB')
                    chunks.append(chunk)
                raw=b''.join(chunks)
                html=raw.decode(response.encoding or 'utf-8',errors='replace')
                body,status,metadata=extract_html(html,current,feed_text)
                return {'html':html,'text':body,'status':status,'metadata':metadata,'error':''}
        raise ValueError('Too many redirects')

def hydrate(document_id, proxy=None):
    c=db.connect()
    try:
        row=c.execute('SELECT * FROM source_documents WHERE id=?',(document_id,)).fetchone()
        if not row or row['source_kind']=='telegram' or row['content_status'] in {'extracted','manual'}: return
        c.commit() # never hold a database transaction during HTTP requests
        try: data=fetch(row['source_url'],row['feed_text'] or row['source_text'],proxy)
        except Exception as error:
            data={'html':'','text':'','status':'error','metadata':{},'error':type(error).__name__}
        best=data['text'] if data['status']=='extracted' else row['source_text']
        c.execute('UPDATE source_documents SET full_text=?,raw_html=?,source_text=?,content_status=?,fetch_error=?, '
                  'metadata=?::jsonb,fetched_at=now(),fetch_attempts=fetch_attempts+1,content_hash=? WHERE id=?',
                  (data['text'],data['html'],best,data['status'],data['error'],json.dumps(data['metadata'],ensure_ascii=False),
                   hashlib.sha256(best.encode()).hexdigest(),document_id))
        if data['text']:
            c.execute('INSERT INTO original_versions(source_document_id,sha256,source_text,origin) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                      (document_id,hashlib.sha256(data['text'].encode()).hexdigest(),data['text'],'web-fetch'))
        c.commit()
    finally: c.close()

def segment(text, maximum=90000):
    """Lossless chunk helper; callers retain the original record identity."""
    if not text:return ['']
    return [text[i:i+maximum] for i in range(0,len(text),maximum)]

def require_complete(rows):
    missing=[r['id'] for r in rows if r.get('source_kind') in {'rss','web'} and r.get('content_status') not in {'extracted','manual'}]
    if missing:
        raise RuntimeError(f'Повний текст не отримано для {len(missing)} статей. '
                           f'Приклади ID: {missing[:20]}. Запустіть tools/fetch_articles.py. '
                           'Неповна добірка не буде видана за повний аналіз.')


def pending_classification_count(connection, start, end):
    return connection.execute("""SELECT count(*) FROM source_documents d JOIN sources s ON s.id=d.source_id
       LEFT JOIN info_influence i ON i.source_document_id=d.id
       WHERE s.for_info AND NULLIF(d.published_at,'')::timestamptz>=?
         AND NULLIF(d.published_at,'')::timestamptz<?
         AND length(trim(COALESCE(NULLIF(d.full_text,''),d.source_text,'')))>0
         AND (i.id IS NULL OR i.status='error')""",
       (start.astimezone(timezone.utc), end.astimezone(timezone.utc))).fetchone()[0]


def require_classified(connection,start,end):
    pending=pending_classification_count(connection,start,end)
    if pending:
        raise RuntimeError(f'{pending} оригіналів інфовпливу за період ще не пройшли первинний відбір. '
                           'Запустіть tools/fetch_articles.py --all і tools/classify_pending.py з потрібними --start/--end.')
