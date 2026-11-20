"""PostgreSQL boundary for the existing editorial engines.

Keeps qmark parameters and named/indexed rows at this boundary only. The
database is PostgreSQL; SQLite is used only by the offline import tool.
"""
import os
import re
from pathlib import Path
import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)

def row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))

class Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None
    def fetchone(self): return self.cursor.fetchone()
    def fetchall(self): return self.cursor.fetchall()
    def __iter__(self): return iter(self.cursor)

class Connection:
    def __init__(self, schema='public', readonly=False):
        if schema not in {'public', 'weekly', 'daily'}: raise ValueError('Unknown schema')
        load_dotenv(ROOT / '.env')
        dsn = os.getenv('DATABASE_URL', '')
        if not dsn: raise RuntimeError('Заповніть DATABASE_URL у .env')
        self.raw = psycopg.connect(dsn, row_factory=row_factory, autocommit=False)
        self.raw.execute(f'SET search_path TO {schema}, public')
        self.raw.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ '+('ONLY' if readonly else 'WRITE'))
        self.raw.commit()
        self.readonly = readonly
    def execute(self, query, parameters=None):
        query = query.strip().rstrip(';')
        if query == 'BEGIN IMMEDIATE': query = 'BEGIN'
        # Only these explicit legacy UPSERTs are allowed.
        keys = {'messages': ['chat_id', 'message_id'], 'rss_entries': ['entry_key'],
                'stage_cache': ['stage','input_hash','prompt_version']}
        if query.upper().startswith('INSERT OR REPLACE'):
            match = re.match(r'INSERT OR REPLACE INTO (\w+)\s*\((.*?)\)',query,re.S)
            table, fields = match.groups()
            pk = keys[table]
            fields = [x.strip() for x in fields.split(',')]
            query = query.replace('INSERT OR REPLACE', 'INSERT', 1)
            query += ' ON CONFLICT ('+','.join(pk)+') DO UPDATE SET '+','.join(
                f'{x}=excluded.{x}' for x in fields if x not in pk)
        elif query.upper().startswith('INSERT OR IGNORE'):
            query = query.replace('INSERT OR IGNORE', 'INSERT', 1)+' ON CONFLICT DO NOTHING'
        inserted = re.match(r'INSERT INTO (\w+)\s*\(',query,re.I)
        generated = bool(inserted and inserted.group(1) in {'news','runs'} and 'RETURNING' not in query.upper())
        if generated: query += ' RETURNING id'
        # qmarks occur only outside SQL strings. Literal percentages must be
        # escaped for psycopg when bound arguments are supplied.
        if parameters is not None:
            parts = re.split(r"('(?:''|[^'])*')", query)
            query = ''.join(p.replace('%','%%').replace('?', '%s') if i%2==0
                            else p.replace('%','%%') for i,p in enumerate(parts))
        try:
            result = Result(self.raw.execute(query, parameters))
            if generated:
                row = result.fetchone()
                result.lastrowid = row[0] if row else None
            return result
        except Exception:
            self.raw.rollback()
            raise
    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()

def connect(schema='public', readonly=False): return Connection(schema, readonly)

def has_table(connection, table):
    return connection.execute('SELECT to_regclass(?) AS name',(table,)).fetchone()['name'] is not None

def install_schema():
    c = connect()
    try:
        c.raw.execute((ROOT/'database/schema.sql').read_text(encoding='utf-8'))
        c.raw.execute((ROOT/'database/extensions.sql').read_text(encoding='utf-8'))
        dzen_schema = ROOT/'database/dzen_intelligence_v1.sql'
        if dzen_schema.exists():
            c.raw.execute(dzen_schema.read_text(encoding='utf-8'))
        c.commit()
    finally: c.close()
