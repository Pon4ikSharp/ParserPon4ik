import asyncio
import json
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from telegram.error import TelegramError
from app import analyst,control_panel,db,articles,collector,daily,editor,weekly,workflow
from app.delivery import replace_news

@pytest.fixture(scope='session')
def pg_connection():
    if os.getenv('PONCHIK_TEST_DB')!='1':pytest.skip('Set PONCHIK_TEST_DB=1 only for an isolated restored test database')
    c=db.connect()
    yield c
    c.rollback();c.close()

@pytest.fixture
def connection(pg_connection):
    yield pg_connection
    pg_connection.rollback()

def test_migration_counts_and_links(connection):
    report=json.loads((db.ROOT/'database/migration_report.json').read_text())
    for table,total in report['counts'].items():
        assert connection.execute('SELECT count(*) FROM '+table).fetchone()[0]==total
    assert connection.execute('SELECT count(*) FROM info_influence i LEFT JOIN source_documents d ON d.id=i.source_document_id WHERE d.id IS NULL').fetchone()[0]==0
    assert connection.execute("SELECT count(*) FROM source_documents WHERE lower(source_url) LIKE '%t.me/insiderukr/%' OR lower(source_url) LIKE '%t.me/voynareal/%'").fetchone()[0]==0
    assert connection.execute("SELECT count(*) FROM sources WHERE source_key IN ('telegram:insiderukr','telegram:voynareal')").fetchone()[0]==0
    assert connection.execute("SELECT count(*) FROM news WHERE source_title LIKE '%,%' AND (source_title LIKE '%INSIDER%' OR source_title LIKE '%Реальна%')").fetchone()[0]>0
    assert connection.execute('SELECT count(*) FROM source_documents d WHERE NOT EXISTS (SELECT 1 FROM original_versions v WHERE v.source_document_id=d.id AND v.source_text=d.source_text)').fetchone()[0]==0

def test_folder_override_and_search(connection):
    row=connection.execute("SELECT * FROM sources WHERE source_key='web:ukraina.ru'").fetchone()
    assert row['audiences']==['РФ'] # user's actual folder, not inferred from its name
    row=connection.execute("SELECT * FROM sources WHERE source_key='telegram:legitimniy'").fetchone()
    assert row['audiences']==['Україна'] and row['for_info']
    assert connection.execute("SELECT count(*) FROM originals_search WHERE to_tsvector('simple',source_text) @@ plainto_tsquery('simple',?)",('Украина',)).fetchone()[0]>0

def test_postgres_write_paths(connection):
    c=connection
    collector.record_message(c,-999000,1,'test','Test','https://t.me/test/1','rejected','one')
    collector.record_message(c,-999000,1,'test','Test','https://t.me/test/1','accepted','two')
    assert c.execute('SELECT reason FROM messages WHERE chat_id=?',(-999000,)).fetchone()[0]=='two'
    collector.set_system_state(c,'__test_state','works')
    assert collector.get_system_state(c,'__test_state')=='works'
    collector.record_detected_event(c,'__test_event','event','test','https://example.com')
    collector.record_detected_event(c,'__test_event','event','test','https://example.com')
    assert collector.detected_event_seen(c,'__test_event')
    c.execute('DELETE FROM messages WHERE chat_id=?',(-999000,))
    c.execute('DELETE FROM detected_events WHERE event_key=?',('__test_event',))
    c.execute('DELETE FROM system_state WHERE key=?',('__test_state',));c.commit()

def test_source_archive_and_classifier(connection):
    c=connection
    original='Повний оригінал\n#хештег\n«Пряма цитата»\n'+'Текст. '*3000
    doc_id=collector.record_source_document(c,source_kind='telegram',source_chat_id=-999001,source_message_id=1,
        source_title='Легитимный',source_url='https://t.me/legitimniy/999999999',source_text=original,published_at=datetime.now(timezone.utc))
    row=c.execute('SELECT * FROM source_documents WHERE id=?',(doc_id,)).fetchone()
    assert row['source_text']==original and row['content_status']=='telegram' and row['source_id']
    analysis={'relevant':True,'role':'carrier','narrative_title':'Тест','summary':'Тестова теза','target_entities':['Україна'],
        'techniques':[],'claims':['Теза'],'strategic_objectives':[],'audiences':['Україна'],'actors':[],
        'information_triggers':[],'future_relevance':'','confidence':90,'narrative_key':'test','reason':'test'}
    collector.record_info_influence(c,source_kind='telegram',source_chat_id=-999001,source_message_id=1,digest='hash',
        source_title='Test',source_url='https://t.me/legitimniy/999999999',analysis=analysis,status='accepted',reason='test')
    start=datetime.now(timezone.utc)-timedelta(minutes=1);end=start+timedelta(minutes=2)
    selected,stats=weekly.fetch_database_materials(c,start,end)
    chosen=next(x for x in selected if x['id']==doc_id)
    assert chosen['source_text']==original and weekly.source_payload(chosen)['text']==original
    rows,_=daily.fetch_documents(c,start,end)
    assert daily.document_payload(next(x for x in rows if x['id']==doc_id))['text']==original
    c.execute('DELETE FROM info_influence WHERE source_document_id=?',(doc_id,))
    c.execute('DELETE FROM source_documents WHERE id=?',(doc_id,));c.commit()

def test_partial_articles_are_not_silently_used():
    with pytest.raises(RuntimeError,match='Повний текст'):
        articles.require_complete([{'id':1,'source_kind':'rss','content_status':'pending'}])
    text='A'*200000+'#ОСТАННІЙ_ХЕШТЕГ'
    assert ''.join(articles.segment(text))==text

def test_html_extraction_keeps_quotes_tags():
    body=' '.join(['Це змістовний текст статті про події та їхні наслідки.']*30)
    html=f'<html><head><title>Повна стаття</title><meta property="article:tag" content="Україна"></head><body><article><h1>Повна стаття</h1><p>{body}</p><blockquote>Пряма цитата #важливо</blockquote><p>{body}</p></article></body></html>'
    text,status,meta=articles.extract_html(html,'https://example.com/a')
    assert status=='extracted' and 'Пряма цитата' in text and '#важливо' in text
    assert meta['tags']==['Україна'] and meta['blockquotes']==['Пряма цитата #важливо']
    _,status,_=articles.extract_html(html.replace('<article>','<div class="paywall"></div><article>'),'https://example.com/a')
    assert status=='paywall'

def test_weekly_cache_and_daily_run_ids(connection):
    if os.getenv('PONCHIK_TEST_DB')!='1':pytest.skip('isolated PG only')
    c=connection
    try:
        c.raw.execute('SET search_path TO weekly, public');c.commit()
        c.execute('INSERT OR REPLACE INTO stage_cache(stage,input_hash,prompt_version,result_json,created_at) VALUES (?,?,?,?,?)',
                  ('__test','hash','v3','{}',datetime.now(timezone.utc).isoformat()))
        assert c.execute('SELECT result_json FROM stage_cache WHERE stage=?',('__test',)).fetchone()[0]=='{}'
        c.execute('DELETE FROM stage_cache WHERE stage=?',('__test',));c.commit()
    finally:
        c.raw.execute('SET search_path TO public');c.commit()
    try:
        c.raw.execute('SET search_path TO daily, public');c.commit()
        start=datetime(2050,1,1,tzinfo=timezone.utc);end=start+timedelta(days=1)
        run=daily.get_or_create_run(c,start,end,False)
        assert run>0 and daily.get_or_create_run(c,start,end,False)==run
        c.execute('DELETE FROM runs WHERE id=?',(run,));c.commit()
    finally:
        c.raw.execute('SET search_path TO public');c.commit()

class Bot:
    def __init__(self,fail=False):self.actions=[];self.fail=fail;self.number=100000
    async def send_message(self,**kw):
        self.actions.append(('send',kw['chat_id']))
        if self.fail:raise TelegramError('simulated network failure')
        self.number+=1
        return SimpleNamespace(message_id=self.number)
    async def delete_message(self,**kw):self.actions.append(('delete',kw['chat_id'],kw['message_id']))
    async def send_animation(self,**kw):
        self.actions.append(('animation',kw['chat_id'],kw['filename']))

def make_note(c,msg,previous=None):
    analysis=dict(category='Інформаційна сфера',paragraph='Тестове повідомлення про важливу заяву.',reference_text='',
                  event_key='__test',facts_key=f'факти {msg}',article_key='__test',primary_sources=[])
    return collector.record_news(c,-999002,msg,analysis,'Test','https://example.com/test',previous)

@pytest.mark.parametrize('failure,old',[(False,False),(True,False),(False,True)])
def test_replacement_order_failure_and_age(connection,failure,old):
    c=connection;chat=999000999
    collector.subscribe_user(c,chat,'test','test')
    first=make_note(c,1)
    c.execute('INSERT INTO telegram_deliveries(news_id,chat_id,telegram_message_id,sent_at) VALUES (?,?,?,?)',
              (first,chat,99999,datetime.now(timezone.utc)-timedelta(hours=49 if old else 1)))
    if old:c.execute('UPDATE news SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=49)).isoformat(),first))
    c.commit()
    previous=c.execute('SELECT * FROM news WHERE id=?',(first,)).fetchone()
    second=make_note(c,2,previous)
    bot=Bot(failure)
    result=asyncio.run(replace_news(SimpleNamespace(bot=bot),c,second,'Текст',{chat},[first]))
    if old:assert bot.actions==[] and result==(0,0)
    elif failure:assert bot.actions==[('send',chat)] and result==(0,1)
    else:
        assert bot.actions==[('send',chat),('delete',chat,99999)] and result==(1,0)
        asyncio.run(replace_news(SimpleNamespace(bot=bot),c,second,'Текст',{chat},[first]))
        assert len(bot.actions)==2 # retry doesn't send the same version again
    c.execute('DELETE FROM telegram_deliveries WHERE chat_id=?',(chat,))
    c.execute('DELETE FROM news_history_links WHERE news_id IN (?,?) OR parent_id IN (?,?)',(first,second,first,second))
    c.execute('DELETE FROM news WHERE chat_id=?',(-999002,))
    c.execute('DELETE FROM subscribers WHERE chat_id=?',(chat,));c.commit()


def test_same_note_new_revision(connection):
    c=connection;chat=999000998
    collector.subscribe_user(c,chat,'test','test')
    note=make_note(c,3);bot=Bot()
    assert asyncio.run(replace_news(SimpleNamespace(bot=bot),c,note,'Version one',{chat}))==(1,0)
    assert asyncio.run(replace_news(SimpleNamespace(bot=bot),c,note,'Version two',{chat}))==(1,0)
    assert bot.actions==[('send',chat),('send',chat),('delete',chat,100001)]
    c.execute('UPDATE news SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=49)).isoformat(),note));c.commit()
    assert asyncio.run(replace_news(SimpleNamespace(bot=bot),c,note,'Version three',{chat}))==(0,0)
    assert len(bot.actions)==3
    c.execute('DELETE FROM telegram_deliveries WHERE chat_id=?',(chat,))
    c.execute('DELETE FROM news WHERE id=?',(note,))
    c.execute('DELETE FROM subscribers WHERE chat_id=?',(chat,));c.commit()


def test_preserve_manual_full_article_and_retry_classification(connection):
    c=connection
    data=dict(source_kind='rss',source_chat_id=-999003,source_message_id=1,source_title='Test',
              source_url='https://ukraina.ru/test',published_at=datetime.now(timezone.utc))
    doc=collector.record_source_document(c,source_text='RSS original',**data)
    full='Повна стаття «цитата» #останнямітка '+('зміст '*500)
    c.execute("UPDATE source_documents SET source_text=?,full_text=?,content_status='manual',content_hash='fullhash' WHERE id=?",(full,full,doc));c.commit()
    collector.record_source_document(c,source_text='Updated RSS excerpt',**data)
    row=c.execute('SELECT * FROM source_documents WHERE id=?',(doc,)).fetchone()
    assert row['source_text']==full and row['full_text']==full and row['content_hash']=='fullhash'
    assert row['feed_text']=='Updated RSS excerpt'
    articles.require_complete([row])
    collector.record_info_influence(c,source_kind='rss',source_chat_id=-999003,source_message_id=1,digest='current',
        source_title='Test',source_url=data['source_url'],analysis={},status='rejected',reason='test')
    assert collector.info_source_item_seen(c,'rss',-999003,1,'current')
    assert not collector.info_source_item_seen(c,'rss',-999003,1,'changed')
    c.execute("UPDATE info_influence SET status='error' WHERE source_document_id=?",(doc,));c.commit()
    assert not collector.info_source_item_seen(c,'rss',-999003,1,'current')
    with pytest.raises(RuntimeError,match='первинний відбір'):
        now=datetime.now(timezone.utc)
        articles.require_classified(c,now-timedelta(minutes=1),now+timedelta(minutes=1))
    c.execute('DELETE FROM info_influence WHERE source_document_id=?',(doc,))
    c.execute('DELETE FROM source_documents WHERE id=?',(doc,));c.commit()


def test_three_role_queues_and_calendar(connection, tmp_path):
    c=connection
    doc=collector.record_source_document(
        c,source_kind='telegram',source_chat_id=-999004,source_message_id=1,
        source_title='Test',source_url='https://example.com/event',
        source_text='Зустріч відбудеться 10 вересня 2026 року.',
        published_at=datetime.now(timezone.utc),
    )
    analysis={'relevant':True,'category':'Інформаційна сфера','paragraph':'Чернетка',
              'reference_text':'','event_key':'queue-test','facts_key':'new facts',
              'article_key':'queue-test','primary_sources':[]}
    draft=workflow.enqueue_note_draft(
        c,source_kind='telegram',source_chat_id=-999004,source_message_id=1,
        source_title='Test',source_url='https://example.com/event',analysis=analysis,
    )
    assert draft['status']=='queued'
    claimed=workflow.claim(c,'note_drafts')
    assert claimed['id']==draft['id']
    workflow.finish(c,'note_drafts',draft['id'],'rejected')
    candidate=workflow.enqueue_event_candidate(
        c,source_kind='telegram',source_chat_id=-999004,source_message_id=1,
        candidate_key='test-meeting',candidate_text='#подія Тестова зустріч',
        source_url='https://example.com/event',payload={'test':True},note_draft_id=draft['id'],
    )
    claimed_event=workflow.claim(c,'event_candidates')
    reviewed={'action':'new','canonical_key':'test-meeting','title':'Тестова зустріч',
              'summary':'Перевірена подія','starts_at':'2026-09-10T10:00:00+03:00',
              'ends_at':'','all_day':False,'date_precision':'exact','status':'confirmed',
              'location':'Київ','participants':['Учасник'],'audiences':['Україна'],
              'tags':['тест'],'confidence':95,'reason':'Пряма дата в оригіналі'}
    event_id=editor.save_event(c,claimed_event,reviewed,None,'gpt-5.6-sol')
    assert c.execute('SELECT status FROM calendar_events WHERE id=?',(event_id,)).fetchone()[0]=='confirmed'
    assert c.execute('SELECT count(*) FROM calendar_event_revisions WHERE event_id=?',(event_id,)).fetchone()[0]==1
    job=workflow.submit_analysis_job(c,'calendar','',datetime(2026,9,1,tzinfo=timezone.utc),datetime(2026,10,1,tzinfo=timezone.utc),{})
    row=c.execute('SELECT * FROM analysis_jobs WHERE id=?',(job,)).fetchone()
    output,result=analyst.calendar_export(c,row,tmp_path)
    assert output.is_file() and result['event_count']>=1
    c.execute('DELETE FROM analysis_jobs WHERE id=?',(job,))
    c.execute('DELETE FROM calendar_events WHERE id=?',(event_id,))
    c.execute('DELETE FROM event_candidates WHERE id=?',(candidate['id'],))
    c.execute('DELETE FROM note_drafts WHERE id=?',(draft['id'],))
    c.execute('DELETE FROM source_documents WHERE id=?',(doc,));c.commit()


def test_clean_shutdown_sends_maintenance_gif(connection):
    c=connection;chat=999000997
    collector.subscribe_user(c,chat,'test','test')
    bot=Bot()
    result=asyncio.run(collector.broadcast_maintenance_animation(
        SimpleNamespace(bot=bot),c,{chat}
    ))
    assert result==(1,0)
    assert bot.actions==[('animation',chat,'donut-server-final.gif')]
    c.execute('DELETE FROM subscribers WHERE chat_id=?',(chat,));c.commit()


def test_local_control_panel_reads_database_and_protects_actions(connection,monkeypatch):
    class SharedConnection:
        def __getattr__(self,name):return getattr(connection,name)
        def close(self):pass
    monkeypatch.setattr(control_panel,'connect',lambda readonly=True:SharedConnection())
    with TestClient(control_panel.app) as client:
        for path in ('/','/originals','/sources','/queues','/calendar','/notes','/analysis','/models','/logs'):
            response=client.get(path)
            assert response.status_code==200, (path,response.text[:500])
            assert 'Лише цей сервер' in response.text
        assert client.post('/api/process/not-a-role/start').status_code==403
        source=connection.execute("SELECT id,enabled FROM sources WHERE source_key='telegram:legitimniy'").fetchone()
        headers={'X-Ponchik-Token':control_panel.app.state.action_token}
        response=client.patch(f"/api/sources/{source['id']}",headers=headers,json={'enabled':not source['enabled']})
        assert response.status_code==200
        changed=connection.execute('SELECT enabled FROM sources WHERE id=?',(source['id'],)).fetchone()[0]
        assert changed is (not source['enabled'])
        client.patch(f"/api/sources/{source['id']}",headers=headers,json={'enabled':source['enabled']})
