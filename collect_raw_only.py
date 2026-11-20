import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app import collector, dzen_intelligence

RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
HEARTBEAT_PATH = RUNTIME_DIR / "parser_heartbeat.json"


def load_rss_highwater(database) -> dict[str, datetime]:
    """Load each feed's newest archived publication once at collector startup."""
    rows = database.execute(
        """SELECT feed_url,max(NULLIF(published_at,'')::timestamptz) AS max_published_at
             FROM source_documents
            WHERE source_kind='rss' AND NULLIF(feed_url,'') IS NOT NULL
              AND NULLIF(published_at,'')::timestamptz<=now()+interval '5 minutes'
            GROUP BY feed_url"""
    ).fetchall()
    result = {}
    for row in rows:
        latest = row["max_published_at"]
        if latest is None:
            continue
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        result[row["feed_url"]] = latest.astimezone(timezone.utc)
    return result


def rss_feed_since(
    highwater: dict[str, datetime],
    feed_url: str,
    startup_floor: datetime,
    overlap_minutes: int,
) -> datetime:
    """Return a safe per-feed cutoff; failed feeds never advance it."""
    latest = highwater.get(feed_url)
    if latest is None:
        return startup_floor
    return latest - timedelta(minutes=max(1, overlap_minutes))


async def main():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    session_stats = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "telegram_saved": 0,
        "rss_saved": 0,
        "last_telegram_at": "",
        "last_rss_cycle_at": "",
        "last_rss_saved_at": "",
        "last_dzen_cycle_at": "",
        "last_dzen_fetched": 0,
    }

    def write_heartbeat(status="running"):
        payload = {
            "status": status,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            **session_stats,
        }
        temporary = HEARTBEAT_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(HEARTBEAT_PATH)

    async def heartbeat_loop():
        while True:
            write_heartbeat("running")
            await asyncio.sleep(10)

    write_heartbeat("starting")
    heartbeat_task = asyncio.create_task(heartbeat_loop())

    # ---------------------------------------------------------
    # БАЗА + TELEGRAM
    # ---------------------------------------------------------
    collector._PROXY_URL, proxy_description = collector.configure_proxy()

    api_id = int(collector.required_env("TELEGRAM_API_ID"))
    api_hash = collector.required_env("TELEGRAM_API_HASH")
    phone = collector.required_env("TELEGRAM_PHONE")

    session_name = os.getenv("SESSION_NAME", "telegram_forwarder").strip()
    session_path = collector.BASE_DIR / session_name

    telegram_client = collector.TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        proxy=collector.telethon_proxy_settings(),
    )

    database = collector.init_db()

    print("RAW COLLECT ONLY")
    print("Codex: OFF")
    print("BR/INFO analysis: OFF")
    print("Editor/Analyst: OFF")
    print("Telegram distribution: OFF")
    print()

    print("Підключення до Telegram...")
    await telegram_client.start(phone=phone)
    print("Telegram авторизований.")

    briefing_channels = collector.read_channels()
    info_channels = collector.read_info_channels()
    all_channels = list(dict.fromkeys(briefing_channels + info_channels))

    channel_ids = {}

    for name in all_channels:
        cid = await collector.resolve_telegram_channel(
            telegram_client,
            name,
        )
        if cid is not None:
            channel_ids[name] = cid
            print(f"TG OK: {name}")

    monitored_ids = set(channel_ids.values())
    briefing_ids = {
        channel_ids[name] for name in briefing_channels if name in channel_ids
    }
    info_ids = {
        channel_ids[name] for name in info_channels if name in channel_ids
    }

    startup_lookback = max(
        30,
        int(os.getenv("TELEGRAM_STARTUP_LOOKBACK_MINUTES", "120")),
    )
    recovered = await collector.archive_missed_telegram_originals(
        telegram_client,
        database,
        all_channels,
        channel_ids,
        force_lookback_minutes=startup_lookback,
    )
    print(
        f"TG STARTUP RECOVERY | lookback={startup_lookback}m | "
        f"processed={recovered}"
    )

    started_at = datetime.now(timezone.utc) - timedelta(seconds=3)

    @telegram_client.on(collector.events.NewMessage(incoming=True))
    async def on_message(event):
        if event.chat_id not in monitored_ids:
            return

        message = event.message

        if message.date and message.date < started_at:
            return

        try:
            chat = await event.get_chat()

            source_title = collector.canonical_source_title(
                getattr(chat, "title", None)
                or getattr(chat, "username", None)
                or str(event.chat_id)
            )

            username = getattr(chat, "username", None)

            source_url = (
                f"https://t.me/{username}/{message.id}"
                if username
                else ""
            )

            source_text = (event.raw_text or "").strip()
            published_at = message.date or datetime.now(timezone.utc)

            doc_id = collector.record_source_document(
                database,
                source_kind="telegram",
                source_chat_id=event.chat_id,
                source_message_id=message.id,
                source_title=source_title,
                source_url=source_url,
                source_text=source_text,
                published_at=published_at,
            )

            session_stats["telegram_saved"] += 1
            session_stats["last_telegram_at"] = datetime.now(timezone.utc).isoformat()
            print(
                f"TG SAVE #{doc_id}: "
                f"{source_title} | {source_url}"
            )

        except Exception as e:
            print(f"TG ERROR: {e}")


    # ---------------------------------------------------------
    # RSS — ТІЛЬКИ АРХІВУЄМО, БЕЗ HYDRATE / AI
    # ---------------------------------------------------------
    async def rss_loop():
        interval = max(
            60,
            int(os.getenv("RSS_POLL_INTERVAL", "300"))
        )

        # For a source never seen before, keep a small startup lookback.
        # Once a feed has archived items, its own newest published timestamp
        # becomes the persistent cursor. This prevents one failing feed from
        # losing articles while other feeds keep advancing normally.
        startup_rss_lookback = max(
            30,
            int(os.getenv("RSS_STARTUP_LOOKBACK_MINUTES", "120")),
        )
        rss_overlap_minutes = max(
            5,
            int(os.getenv("RSS_FEED_OVERLAP_MINUTES", "360")),
        )
        rss_highwater = load_rss_highwater(database)

        while True:
            startup_floor = datetime.now(timezone.utc) - timedelta(
                minutes=startup_rss_lookback
            )
            br_feeds = collector.read_rss_feeds()
            info_feeds = collector.read_info_rss_feeds()
            info_web = collector.sources.web_only_endpoints("info")
            info_feeds = list(dict.fromkeys(info_feeds + info_web))
            feeds = list(dict.fromkeys(br_feeds + info_feeds))

            saved_total = 0

            for feed_url in feeds:
                try:
                    title, entries = await asyncio.to_thread(
                        collector.fetch_rss_feed,
                        feed_url,
                    )

                    feed_since = rss_feed_since(
                        rss_highwater,
                        feed_url,
                        startup_floor,
                        rss_overlap_minutes,
                    )
                    fresh = []

                    for entry in entries:
                        pub = entry.get("published_at")

                        # Per-feed persistent cursor: one broken RSS endpoint
                        # cannot make us skip its missed items after recovery.
                        if pub is not None:
                            if pub.tzinfo is None:
                                pub = pub.replace(tzinfo=timezone.utc)
                            pub = pub.astimezone(timezone.utc)
                            if pub < feed_since:
                                continue

                        fresh.append(entry)

                    if fresh:
                        collector.record_rss_source_documents(
                            database,
                            feed_url,
                            title,
                            fresh,
                        )
                        valid_times = []
                        future_limit = datetime.now(timezone.utc) + timedelta(minutes=5)
                        for fresh_entry in fresh:
                            item_time = fresh_entry.get("published_at")
                            if item_time is None:
                                continue
                            if item_time.tzinfo is None:
                                item_time = item_time.replace(tzinfo=timezone.utc)
                            item_time = item_time.astimezone(timezone.utc)
                            if item_time <= future_limit:
                                valid_times.append(item_time)
                        if valid_times:
                            latest_seen = max(valid_times)
                            current = rss_highwater.get(feed_url)
                            if current is None or latest_seen > current:
                                rss_highwater[feed_url] = latest_seen

                        saved_total += len(fresh)
                        session_stats["rss_saved"] += len(fresh)
                        session_stats["last_rss_saved_at"] = datetime.now(timezone.utc).isoformat()

                        print(
                            f"RSS SAVE: {title} | "
                            f"items={len(fresh)}"
                        )

                    try:
                        collector.update_rss_feed_state(
                            database,
                            feed_url,
                            title,
                        )
                    except Exception:
                        pass

                except Exception as e:
                    print(f"RSS ERROR {feed_url}: {e}")

            session_stats["last_rss_cycle_at"] = datetime.now(timezone.utc).isoformat()
            write_heartbeat("running")
            print(
                f"RSS CYCLE DONE | candidates={saved_total} | "
                f"next={interval}s"
            )

            await asyncio.sleep(interval)


    async def dzen_loop():
        interval = max(180, int(os.getenv("DZEN_POLL_INTERVAL", "300")))
        while True:
            try:
                result = await asyncio.to_thread(
                    dzen_intelligence.run_live_discovery, fetch_score=4, fetch_limit=80
                )
                session_stats["last_dzen_cycle_at"] = datetime.now(timezone.utc).isoformat()
                session_stats["last_dzen_fetched"] = int(result.get("fetched") or 0)
                print(
                    "DZEN CYCLE | "
                    f"discovered={result.get('discovered', 0)} | "
                    f"candidates={result.get('candidate_count', 0)} | "
                    f"fetched={result.get('fetched', 0)} | "
                    f"errors={result.get('errors', 0)}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"DZEN ERROR: {error}")
            await asyncio.sleep(interval)

    rss_task = asyncio.create_task(rss_loop())
    dzen_task = asyncio.create_task(dzen_loop())
    telegram_channels_task = asyncio.create_task(
        collector.supervise_background_task(
            "telegram_channels",
            collector.telegram_channels_scheduler,
            telegram_client,
            database,
            monitored_ids,
            briefing_ids,
            info_ids,
            channel_ids,
        )
    )

    print()
    print(
        f"Слухаю Telegram: {len(monitored_ids)} каналів."
    )
    print(
        "RSS працює окремим циклом."
    )
    print(
        "Для зупинки: Ctrl+C"
    )
    print()

    try:
        await telegram_client.run_until_disconnected()
    finally:
        rss_task.cancel()
        dzen_task.cancel()
        heartbeat_task.cancel()
        telegram_channels_task.cancel()

        try:
            await rss_task
        except asyncio.CancelledError:
            pass
        try:
            await dzen_task
        except asyncio.CancelledError:
            pass
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await telegram_channels_task
        except asyncio.CancelledError:
            pass

        write_heartbeat("stopped")
        await telegram_client.disconnect()
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
