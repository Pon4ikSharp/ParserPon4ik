@echo off
cd /d C:\ParserPon4ik_PostgreSQL\ParserPon4ik
.venv\Scripts\python.exe scripts\hold_v2_sync.py >> runtime\hold_v2_sync.log 2>&1
