@echo off
cd /d C:\ParserPon4ik_PostgreSQL\ParserPon4ik
.venv\Scripts\python.exe scripts\grok_local_v3_watch.py >> runtime\logs\grok_local_v3_watch_task.log 2>&1

