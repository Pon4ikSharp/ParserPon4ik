"""Discover publisher-advertised RSS/Atom feeds for registered websites."""
import sys
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db,articles,collector

def main():
    c=db.connect();proxy,_=collector.configure_proxy();collector._PROXY_URL=proxy
    try:
        rows=c.execute("""SELECT s.* FROM sources s WHERE s.kind='web' AND s.enabled
           AND NOT EXISTS(SELECT 1 FROM source_endpoints e WHERE e.source_id=s.id AND e.kind='rss' AND e.enabled)""").fetchall();c.commit()
        for row in rows:
            try:
                page=articles.fetch(row['url'],proxy=proxy)
                soup=BeautifulSoup(page['html'],'html.parser');found=False
                for link in soup.select('link[rel="alternate"][href]'):
                    if link.get('type','').lower() not in {'application/rss+xml','application/atom+xml'}:continue
                    url=articles.public_url(urljoin(row['url'],link['href']))
                    collector.fetch_rss_feed(url)
                    c.execute("INSERT INTO source_endpoints(endpoint,source_id,kind) VALUES (?,?,'rss') ON CONFLICT DO NOTHING",(url,row['id']));c.commit();found=True
                print(row['source_key'],'RSS підключено' if found else 'RSS не оголошено: потрібне окреме підключення')
            except Exception as error:
                c.rollback();print(row['source_key'],type(error).__name__)
    finally:c.close()
if __name__=='__main__':main()
