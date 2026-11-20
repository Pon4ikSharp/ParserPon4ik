"""Track each recipient's copies; delete only after the replacement is saved."""
import asyncio
import hashlib
from datetime import datetime,timedelta,timezone
from telegram import LinkPreviewOptions
from telegram.error import Forbidden,RetryAfter,TelegramError

async def send(bot,chat_id,text):
    try:
        return await bot.send_message(chat_id=chat_id,text=text,parse_mode='HTML',
                                      link_preview_options=LinkPreviewOptions(is_disabled=True))
    except RetryAfter as error:
        delay=error.retry_after
        await asyncio.sleep((delay.total_seconds() if hasattr(delay,'total_seconds') else float(delay))+1)
        return await bot.send_message(chat_id=chat_id,text=text,parse_mode='HTML',
                                      link_preview_options=LinkPreviewOptions(is_disabled=True))

async def replace_news(application,c,news_id,text,allowed,previous_ids=()):
    news=c.execute('SELECT * FROM news WHERE id=?',(news_id,)).fetchone()
    now=datetime.now(timezone.utc)
    body_hash=hashlib.sha256(text.encode('utf-8')).hexdigest()
    # Cutoff is the age of the news, never its last edit time.
    def utc(value):
        if isinstance(value,str):value=datetime.fromisoformat(value)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    # Follow all merged branches and earlier copies, including after a failed send.
    lineage=c.execute('''WITH RECURSIVE history(id) AS (
        SELECT ?::bigint UNION SELECT l.parent_id FROM news_history_links l JOIN history h ON l.news_id=h.id
        ) SELECT id FROM history''',(news_id,)).fetchall()
    previous_ids=sorted(set(previous_ids)|{r['id'] for r in lineage if r['id']!=news_id})
    if previous_ids:
        old=c.execute('SELECT created_at FROM news WHERE id IN ('+','.join('?' for _ in previous_ids)+')',tuple(previous_ids)).fetchall()
        if any(now-utc(r['created_at'])>=timedelta(hours=48) for r in old):
            return 0,0
    delivered=failed=0
    subscribers=c.execute('SELECT chat_id FROM subscribers WHERE is_active=1').fetchall()
    for subscriber in subscribers:
        chat_id=subscriber['chat_id']
        if allowed and chat_id not in allowed: continue
        already=c.execute('SELECT 1 FROM telegram_deliveries WHERE chat_id=? AND news_id=? AND body_hash=? AND deleted_at IS NULL',
                          (chat_id,news_id,body_hash)).fetchone()
        if already:continue
        replacement_ids=sorted(set(previous_ids)|{news_id})
        old=[]
        if replacement_ids:
            old=c.execute('SELECT * FROM telegram_deliveries WHERE chat_id=? AND deleted_at IS NULL AND news_id IN ('+
                          ','.join('?' for _ in replacement_ids)+')',(chat_id,*replacement_ids)).fetchall()
        # An old chat copy is left in place; no new notification for that user.
        if old and (now-utc(news['created_at'])>=timedelta(hours=48) or any(now-utc(r['sent_at'])>=timedelta(hours=48) for r in old)): continue
        c.commit()
        try:
            message=await send(application.bot,chat_id,text)
            c.execute('INSERT INTO telegram_deliveries(news_id,chat_id,telegram_message_id,body_hash) VALUES (?,?,?,?)',
                      (news_id,chat_id,message.message_id,body_hash))
            c.commit()
            delivered+=1
        except Forbidden:
            c.execute('UPDATE subscribers SET is_active=0 WHERE chat_id=?',(chat_id,));c.commit();failed+=1;continue
        except TelegramError:
            failed+=1;continue
        for previous in old:
            try:
                await application.bot.delete_message(chat_id=chat_id,message_id=previous['telegram_message_id'])
                c.execute('UPDATE telegram_deliveries SET deleted_at=now(),delete_error=\'\' WHERE chat_id=? AND telegram_message_id=?',
                          (chat_id,previous['telegram_message_id']))
            except TelegramError as error:
                c.execute('UPDATE telegram_deliveries SET delete_error=? WHERE chat_id=? AND telegram_message_id=?',
                          (type(error).__name__,chat_id,previous['telegram_message_id']))
            c.commit()
    return delivered,failed
