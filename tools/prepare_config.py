"""Copy the existing local configuration/session without disclosing credentials."""
import argparse,shutil,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values,set_key
from app.db import ROOT
p=argparse.ArgumentParser();p.add_argument('--old-dir',type=Path,required=True);a=p.parse_args()
old=a.old_dir.resolve();target=ROOT/'.env'
if old==ROOT:raise ValueError('Вкажіть окрему папку старої програми')
if target.exists():raise RuntimeError('.env уже існує. Відредагуйте його вручну: файл не перезаписано.')
source=old/'.env'
shutil.copy2(source if source.exists() else ROOT/'.env.example',target)
for key in ['CODEX_MODEL','WEEKLY_CODEX_MODEL','INFOSPACE_CODEX_MODEL','ANALYST_MODEL','EDITOR_MODEL']:
    set_key(target,key,'gpt-5.6-sol')
set_key(target,'ESCALATION_MODEL','gpt-6-astra')
set_key(target,'EDITOR_ASTRA_ESCALATION','false')
set_key(target,'RSS_PROCESS_EXISTING','1')
if not dotenv_values(target).get('DATABASE_URL'):set_key(target,'DATABASE_URL','postgresql://postgres:CHANGE_ME@127.0.0.1:5432/ponchik')
name=dotenv_values(target).get('SESSION_NAME') or 'telegram_forwarder'
# Copy only the configured Telethon session inside the old directory.
relative=Path(name)
if not relative.is_absolute() and '..' not in relative.parts:
    relative=Path(str(relative) if str(relative).endswith('.session') else str(relative)+'.session')
    if (old/relative).is_file():
        (ROOT/relative).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old/relative,ROOT/relative)
print('Конфігурацію підготовлено. Заповніть DATABASE_URL у .env. Старий бот має бути зупинений.')
