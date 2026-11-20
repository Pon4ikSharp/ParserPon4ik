"""Offline merge of ALL news SQLite snapshots; produces a PostgreSQL COPY dump.

Input files are opened read-only. Repeated source posts merge by their original
Telegram/RSS identity, never by coincident SQLite IDs. All original text
variants are retained. Newest snapshots win operational state conflicts.
"""
import argparse
import collections
import gzip
import hashlib
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.sources import source_key,telegram_name,excluded

ROOT=Path(__file__).resolve().parents[1]
KEYS={'source_documents':('source_kind','source_chat_id','source_message_id'),
      'info_influence':('source_kind','source_chat_id','source_message_id'),
      'news':('chat_id','message_id'),'messages':('chat_id','message_id'),
      'subscribers':('chat_id',),'detected_events':('event_key',),
      'digest_runs':('window_end',),'rss_entries':('entry_key',),
      'rss_feed_state':('feed_url',),'system_state':('key',)}

def listed(name):
    return {s.strip() for s in (ROOT/name).read_text(encoding='utf-8-sig').splitlines() if s.strip() and not s.lstrip().startswith('#')}

def catalog():
    items={};endpoints={}
    def add(url,title='',group='',kind=None,br=False,info=False):
        if excluded(url):return
        key=source_key(url)
        if key=='web:':return
        row=items.setdefault(key,{'source_key':key,'kind':'telegram' if key.startswith('telegram:') else 'web',
                 'title':title or url,'url':url,'groups':[],'audiences':[],'for_br':False,'for_info':False,
                 'enabled':True,'audience_origin':'unassigned'})
        if title:row['title']=title
        if group and group not in row['groups']:row['groups'].append(group)
        row['for_br']|=br;row['for_info']|=info
        if kind:
            endpoint='@'+telegram_name(url) if kind=='telegram' else url
            endpoints[endpoint]={'endpoint':endpoint,'source_key':key,'kind':kind,'enabled':True}
    # Supplied folders are authoritative. No content-based reassignment.
    text=(ROOT/'database/source_groups.txt').read_text(encoding='utf-8-sig').replace('\\_','_')
    groups={}
    for section in text.split('### ')[1:]:
        group=section.split('|',1)[0].strip().replace('Словваччина','Словаччина')
        for title,url in re.findall(r'\|\s*(?:Telegram|Сайт|Bluesky/RSS)\s*\|\s*([^|]+)\|\s*\[[^]]+\]\(([^)]+)\)',section):
            add(url,title.strip(),group,'telegram' if 't.me/' in url else 'web',
                br=group in {'World','BBC test'},info=group not in {'World','BBC test'})
            groups[source_key(url)]=items[source_key(url)]['groups']
    # Known RSS endpoints from the export, retaining only user-defined grouping
    # when one exists. Monitoring pseudo-feeds are not internet endpoints.
    for n in ET.parse(ROOT/'inoreader_sources.xml').iter('outline'):
        feed=n.get('xmlUrl','');url=n.get('htmlUrl','')
        if not feed.startswith(('https://','http://')):continue
        if source_key(url) in items:
            key=source_key(url)
            endpoints[feed]={'endpoint':feed,'source_key':key,'kind':'rss','enabled':True}
    for file,kind,route in [('channels.txt','telegram','br'),('channels_info.txt','telegram','info'),
                            ('rss_feeds.txt','rss','br'),('rss_feeds_info.txt','rss','info')]:
        for url in sorted(listed(file)):
            if excluded(url):continue
            known=endpoints.get(url,{}).get('source_key')
            if known:
                items[known]['for_'+route]=True
            else:add(url,kind=kind,br=route=='br',info=route=='info')
    for row in items.values():
        groups=row['groups'];aud=[]
        for group in groups:
            a={'Україна':'Україна','РФ':'РФ','ТОТ2':'ТОТ України','Словаччина':'Словаччина',
               'Угорщина':'Угорщина','Білорусь':'Білорусь','Туреччина':'Туреччина','Молдова':'Молдова',
               'Західні':'Країни Заходу','Pravda-Infodef':'Країни Заходу','RT-Sputnik':'Зовнішня аудиторія',
               'World':'Міжнародна аудиторія'}.get(group)
            if a and a not in aud:aud.append(a)
        if 'Словаччина' in aud and 'Країни Заходу' in aud:aud.remove('Країни Заходу')
        row['audiences']=aud
        if aud:row['audience_origin']='user_folders'
    return items,endpoints

def copy_value(value):
    if value is None:return r'\N'
    if isinstance(value,(list,dict)):value=json.dumps(value,ensure_ascii=False)
    if isinstance(value,bool):return 't' if value else 'f'
    return str(value).replace('\\','\\\\').replace('\t','\\t').replace('\n','\\n').replace('\r','\\r').replace('\x00','')

def merge(inputs,output):
    stores={t:{} for t in KEYS};maps={};versions={};origin_rows={};report={'inputs':[],'excluded':collections.Counter()}
    sources,eps=catalog()
    removed_ids=set()
    for path in inputs:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='source_documents'").fetchone():
                removed_ids.update(r[0] for r in c.execute('SELECT source_chat_id,source_url FROM source_documents') if excluded(r[1]))
    # Sources from all snapshots are registered even if not currently polled.
    for path in inputs:
        c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        origin=path.name+'@'+hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        report['inputs'].append({'name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'source_documents':c.execute('SELECT count(*) FROM source_documents').fetchone()[0] if 'source_documents' in tables else 0})
        for table,keys in KEYS.items():
            if table not in tables:continue
            for raw in c.execute('SELECT * FROM '+table):
                row=dict(raw);key=tuple(row[k] for k in keys)
                removed=(row.get('source_chat_id',row.get('chat_id')) in removed_ids or excluded(row.get('source_url','')))
                if table=='news':
                    links=json.loads(row.get('source_links') or '[]')
                    # Existing consolidated notes are explicitly retained unchanged.
                    merged=len({x.get('url') or x.get('title') for x in links})>1 or ',' in row.get('source_title','')
                    removed=removed and not merged
                if removed:
                    report['excluded'][table]+=1;continue
                previous=stores[table].get(key)
                stores[table][key]=(row,origin)
                if 'id' in row:maps[(origin,table,row['id'])]=key
                if table=='source_documents':
                    text=row['source_text'];h=hashlib.sha256(text.encode()).hexdigest()
                    versions[(key,h)]=(text,origin)
                    # Keep the most complete original but current timestamps/metadata.
                    if previous and len(previous[0]['source_text'])>len(text):row['source_text']=previous[0]['source_text']
                    sk=eps.get(row.get('feed_url',''),{}).get('source_key') or source_key(row['source_url'] or row.get('feed_url',''))
                    if sk not in sources:
                        sources[sk]={'source_key':sk,'kind':'telegram' if row['source_kind']=='telegram' else 'web','title':row['source_title'],
                                     'url':row['source_url'],'groups':[],'audiences':[],'for_br':False,'for_info':False,'enabled':False,'audience_origin':'unassigned'}
                    row['_source_key']=sk
        c.close()
    # Sequential independent IDs after merging. Every FK is remapped by origin.
    id_maps={}
    for table,rows in stores.items():
        id_maps[table]={key:i for i,key in enumerate(rows,1)}
    source_ids={key:i for i,key in enumerate(sorted(sources),1)}
    def mapped(origin,table,old):
        key=maps.get((origin,table,old));return id_maps[table].get(key) if key is not None else None
    data={}
    for table,rows in stores.items():
        values=[]
        for key,(raw,origin) in rows.items():
            row=dict(raw)
            if 'id' in row:row['id']=id_maps[table][key]
            if table=='source_documents':
                sk=row.pop('_source_key');row['source_id']=source_ids[sk]
                row.update(feed_text=row['source_text'],full_text='',raw_html='',content_status='telegram' if row['source_kind']=='telegram' else 'pending')
                row['content_hash']=hashlib.sha256(row['source_text'].encode()).hexdigest()
            if table=='info_influence':
                row['source_document_id']=id_maps['source_documents'].get(key)
                if row['source_document_id'] is None:continue
                sk=stores['source_documents'][key][0]['_source_key']
                if sources[sk]['audiences']:row['audiences']=json.dumps(sources[sk]['audiences'],ensure_ascii=False)
                sources[sk]['for_info']=True
            if table=='news':
                row['canonical_id']=mapped(origin,'news',row.get('canonical_id')) or row['id']
                row['supersedes_id']=mapped(origin,'news',row.get('supersedes_id'))
                row.setdefault('reference_text','');row.setdefault('article_key','');row.setdefault('primary_sources','[]')
                row.setdefault('source_links','[]');row.setdefault('is_current',1)
                row['updated_at']=row.get('updated_at') or row['created_at']
            values.append(row)
        data[table]=values
    data['sources']=[{'id':source_ids[k],**sources[k]} for k in sorted(sources)]
    data['source_endpoints']=[{k:v for k,v in {**e,'source_id':source_ids[e['source_key']]}.items() if k!='source_key'} for e in eps.values()]
    data['original_versions']=[dict(source_document_id=id_maps['source_documents'][key],sha256=h,source_text=v[0],origin=v[1]) for (key,h),v in versions.items()]
    data['import_map']=[dict(origin=o,table_name=t,old_id=old,new_id=id_maps[t][key]) for (o,t,old),key in maps.items()]
    # Validate links BEFORE emitting any deliverable.
    docids={r['id'] for r in data['source_documents']};newsids={r['id'] for r in data['news']}
    assert all(r['source_document_id'] in docids for r in data['info_influence'])
    assert all(r['canonical_id'] in newsids and (r['supersedes_id'] is None or r['supersedes_id'] in newsids) for r in data['news'])
    assert not any(excluded(r['source_url']) for r in data['source_documents'])
    report['counts']={t:len(rows) for t,rows in data.items()}
    report['pending_web_articles']=sum(r['content_status']=='pending' for r in data['source_documents'])
    report['unmapped_sources']=sum(not r['audiences'] for r in data['sources'])
    report['web_without_rss']=[r['url'] for r in data['sources'] if r['enabled'] and r['kind']=='web' and not any(e['source_id']==r['id'] and e['kind']=='rss' for e in data['source_endpoints'])]
    output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(output,'wt',encoding='utf-8',newline='\n') as out:
        out.write('BEGIN;\n'+(ROOT/'database/schema.sql').read_text()+'\n'+(ROOT/'database/extensions.sql').read_text()+'\n')
        order=['sources','source_endpoints',*KEYS,'original_versions','import_map']
        for table in order:
            rows=data[table]
            if not rows:continue
            columns=sorted(set().union(*(r.keys() for r in rows)))
            # Fill missing historical columns from the current schema defaults.
            defaults={'source_document_id':None,'updated_at':None,'reference_text':'','article_key':'','primary_sources':'[]',
                      'strategic_objectives':'[]','audiences':'[]','actors':'[]','information_triggers':'[]','future_relevance':'','confidence':0,'classifier_version':''}
            out.write('COPY public.'+table+' ('+','.join('"'+c+'"' for c in columns)+') FROM STDIN;\n')
            for row in rows:out.write('\t'.join(copy_value(row.get(c,defaults.get(c))) for c in columns)+'\n')
            out.write('\\.\n')
        for table in ['sources','source_documents','info_influence','news','detected_events']:
            out.write(f"SELECT setval(pg_get_serial_sequence('public.{table}','id'),COALESCE((SELECT MAX(id) FROM public.{table}),1),EXISTS(SELECT 1 FROM public.{table}));\n")
        out.write('COMMIT;\n')
    report['excluded_count_scope']='Сума вилучених рядків по всіх вхідних знімках; повтори між знімками враховані.'
    report['unmapped_source_details']=[{'source_key':r['source_key'],'title':r['title'],'url':r['url']} for r in data['sources'] if not r['audiences']]
    (output.parent/'migration_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    # Fixture for reproducible SQL validation without a running system service.
    print(json.dumps(report['counts'],ensure_ascii=False))
    return data,report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input-dir',type=Path,required=True);p.add_argument('--output',type=Path,default=ROOT/'database/merged.sql.gz');a=p.parse_args()
    candidates=[]
    for path in a.input_dir.rglob('*.db'):
        if path.name.startswith('news'):candidates.append(path)
    candidates.sort(key=lambda p:(2 if p.name=='news_live.db' else 1 if p.name=='news.db' else 0,re.findall(r'\d{8}_\d{6}',p.name),p.name))
    merge(candidates,a.output)
