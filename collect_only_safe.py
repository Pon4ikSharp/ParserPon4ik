import os
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Штатний режим collector, у якому BR/editor/analyst не запускаються.
os.environ["PRIMARY_ANALYSIS_ONLY"] = "1"

# RSS беремо тільки від моменту запуску (із невеликим запасом).
os.environ["PRIMARY_BACKLOG_SINCE"] = (
    datetime.now(timezone.utc) - timedelta(minutes=15)
).isoformat()

from app import collector


# ------------------------------------------------------------
# КРИТИЧНО: НІЯКОГО CODEX
# ------------------------------------------------------------

# У primary-only collector сам не викликає INFO/BR для нових матеріалів,
# але штатно запускає primary workers. Забираємо їх повністю.
collector._primary_worker_count = lambda: 0


async def idle_forever(*args, **kwargs):
    while True:
        await asyncio.sleep(3600)


# Не даємо статистичному scheduler-у зайвий раз працювати з primary queue.
collector.primary_stats_scheduler = idle_forever

# Не відсіюємо локальним prefilter: зараз тільки ЗБЕРІГАЄМО все.
collector._quick_junk_reason = lambda text: ""

# Навіть якщо якийсь маршрут випадково спробує викликати Codex,
# executable навмисно не існує.
collector.find_codex_executable = lambda: str(
    Path(__file__).with_name("__CODEX_DISABLED__.exe")
)

# Додаткова страховка: прямі request_*codex також заблоковані.
def codex_blocked(*args, **kwargs):
    raise RuntimeError("COLLECT_ONLY_SAFE: Codex disabled")

for name in dir(collector):
    if name.startswith("request_") and "codex" in name.lower():
        setattr(collector, name, codex_blocked)


asyncio.run(collector.main())
