"""Independent Codex profiles; no shared mutable home or cache."""
import os
import shutil
from pathlib import Path
from app.db import ROOT

def profile_home(role):
    aliases={'daily':'analyst','weekly':'analyst'}
    role=aliases.get(role,role)
    if role not in {'collector','analyst','editor'}: raise ValueError(role)
    return ROOT/'codex'/role/'profile'

def environment(role):
    env=os.environ.copy()
    for key in list(env):
        if key in {'DATABASE_URL','BOT_TOKEN','TELEGRAM_API_HASH','TELEGRAM_PHONE','GEMINI_API_KEY'} or key.startswith(('INOREADER_','WEEKLY_INOREADER_')):
            env.pop(key,None)
    home=profile_home(role)
    home.mkdir(parents=True,exist_ok=True)
    env['CODEX_HOME']=str(home)
    return env

def model(role):
    keys={
        'collector':'CODEX_MODEL',
        'daily':'ANALYST_MODEL',
        'weekly':'ANALYST_MODEL',
        'analyst':'ANALYST_MODEL',
        'editor':'EDITOR_MODEL',
    }
    return os.getenv(keys[role],'gpt-5.6-sol').strip()

def escalation_model():
    return os.getenv('ESCALATION_MODEL','gpt-6-astra').strip()

def executable(role):
    """Resolve the role-specific Codex command, including codex.cmd on Windows."""
    keys={'collector':'CODEX_COMMAND','editor':'EDITOR_CODEX_COMMAND','analyst':'ANALYST_CODEX_COMMAND'}
    configured=os.getenv(keys.get(role,''),'').strip()
    if configured:
        resolved=shutil.which(configured)
        if resolved:
            return resolved
        path=Path(configured)
        if path.is_file():
            return str(path)
        raise RuntimeError(f"Не знайдено команду Codex для {role}: {configured}")
    for candidate in ('codex.cmd','codex.exe','codex'):
        resolved=shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError("Не знайдено Codex CLI. Перевірте встановлення командою codex --version")

def command_error(role, completed):
    details=(completed.stderr or completed.stdout or '').strip()
    if len(details)>3500:
        details=details[-3500:]
    headline=f"Codex {role} завершився з кодом {completed.returncode}"
    return f"{headline}: {details}" if details else headline

def is_transient_error(error):
    text=str(error).lower()
    markers=(
        'rate limit','usage limit','quota','too many requests','429','bad gateway','502','capacity',
        '503','504','temporarily unavailable','timed out','timeout','connection reset',
        'network','service unavailable','try again later','ліміт',
    )
    return any(marker in text for marker in markers)
