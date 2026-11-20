import asyncio
from pathlib import Path

from app import collector


async def no_ai_route(*args, **kwargs):
    # Матеріал уже збережений у source_documents.
    # Ніякої ШІ-обробки зараз не виконуємо.
    return "queued", "COLLECT_ONLY_TONIGHT: AI disabled"


async def idle_scheduler(*args, **kwargs):
    # Не запускаємо старі черги BR/INFO.
    while True:
        await asyncio.sleep(3600)


async def no_broadcast(*args, **kwargs):
    # Жодної розсилки підписникам.
    return 0, 0


def blocked_codex(*args, **kwargs):
    raise RuntimeError("COLLECT_ONLY_TONIGHT: Codex is disabled")


# Жорстко відключаємо всі штатні AI-маршрути
collector.process_source_item = no_ai_route
collector.process_info_source_item = no_ai_route

if hasattr(collector, "classification_backlog_scheduler"):
    collector.classification_backlog_scheduler = idle_scheduler

if hasattr(collector, "briefing_backlog_scheduler"):
    collector.briefing_backlog_scheduler = idle_scheduler

if hasattr(collector, "broadcast_to_subscribers"):
    collector.broadcast_to_subscribers = no_broadcast

# Забороняємо будь-які request_*codex, у тому числі додані v6.4
for name in dir(collector):
    low = name.lower()
    if name.startswith("request_") and "codex" in low:
        setattr(collector, name, blocked_codex)

# Додаткова страховка:
# навіть якщо якийсь маршрут випадково дійде до запуску Codex,
# реальний codex.exe йому не буде переданий.
collector.find_codex_executable = lambda: str(
    Path(__file__).with_name("__CODEX_DISABLED__.exe")
)

asyncio.run(collector.main())
