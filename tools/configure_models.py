"""Apply the measured-cost model policy to an existing .env."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import set_key
from app.db import ROOT

path = ROOT / ".env"
if not path.exists():
    raise RuntimeError("Файл .env не знайдений")
for key in ("CODEX_MODEL", "WEEKLY_CODEX_MODEL", "INFOSPACE_CODEX_MODEL",
            "ANALYST_MODEL", "EDITOR_MODEL"):
    set_key(path, key, "gpt-5.6-sol")
set_key(path, "ESCALATION_MODEL", "gpt-6-astra")
set_key(path, "EDITOR_ASTRA_ESCALATION", "false")
print("Усі профілі працюють на GPT-5.6 Sol. Astra доступна лише для посиленого тематичного дослідження за ручним запитом.")
