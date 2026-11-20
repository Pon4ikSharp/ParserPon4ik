from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from langdetect import DetectorFactory, LangDetectException, detect

from app import db

ROOT = db.ROOT
MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
PROVIDER = "mymemory-machine-translation"
TARGET_LANGUAGE = "uk"

DetectorFactory.seed = 0


def detect_source_language(text: str) -> str:
    sample = (text or "").strip()
    if not sample:
        return "auto"

    # Fast deterministic hints before statistical detection.
    lowered = sample.lower()
    if any(ch in lowered for ch in "іїєґ"):
        return "uk"
    if any(ch in lowered for ch in "ыэъё"):
        return "ru"
    if any("\u0600" <= ch <= "\u06ff" for ch in sample):
        return "ar"

    try:
        return detect(sample[:5000])
    except LangDetectException:
        return "auto"


def chunk_text_bytes(text: str, maximum_bytes: int = 450) -> list[str]:
    """Split text under MyMemory's query-size limit without breaking UTF-8."""
    text = (text or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    current = ""
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            if current:
                chunks.append(current.strip())
                current = ""
            continue

        words = paragraph.split()
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate.encode("utf-8")) <= maximum_bytes:
                current = candidate
                continue

            if current:
                chunks.append(current.strip())
                current = ""

            # Very long token/URL: split safely by characters.
            piece = ""
            for ch in word:
                test = piece + ch
                if len(test.encode("utf-8")) > maximum_bytes:
                    if piece:
                        chunks.append(piece)
                    piece = ch
                else:
                    piece = test
            current = piece

        if current:
            chunks.append(current.strip())
            current = ""

    if current:
        chunks.append(current.strip())
    return chunks


def translate_chunk_mymemory(
    text: str,
    source: str,
    target: str = TARGET_LANGUAGE,
) -> str:
    if source == target:
        return text

    if source == "auto":
        source = detect_source_language(text)
    if source in {"auto", "unknown"}:
        raise RuntimeError("Could not determine source language for translation")
    if source == target:
        return text

    params = {
        "q": text,
        "langpair": f"{source}|{target}",
    }
    with httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "ParserPon4ik/1.0"},
    ) as client:
        response = client.get(MYMEMORY_ENDPOINT, params=params)
        response.raise_for_status()
        payload = response.json()

    status = int(payload.get("responseStatus") or response.status_code)
    translated = (
        payload.get("responseData", {}).get("translatedText")
        if isinstance(payload, dict)
        else ""
    )
    if status != 200 or not str(translated or "").strip():
        detail = payload.get("responseDetails") if isinstance(payload, dict) else ""
        raise RuntimeError(f"MyMemory translation failed: {status} {detail}")

    return html.unescape(str(translated)).strip()


def translate_text(
    text: str,
    target: str = TARGET_LANGUAGE,
) -> tuple[str, str, str]:
    source = detect_source_language(text)
    if source == target:
        return (text or "").strip(), "identity-uk", source

    chunks = chunk_text_bytes(text)
    if not chunks:
        return "", PROVIDER, source

    translated = [
        translate_chunk_mymemory(chunk, source, target)
        for chunk in chunks
    ]
    return "\n\n".join(translated).strip(), PROVIDER, source


def claim(connection):
    row = connection.execute(
        """SELECT * FROM document_translations
           WHERE status='queued'
             AND (started_at IS NULL OR started_at < now()-interval '60 seconds')
           ORDER BY created_at,source_document_id
           FOR UPDATE SKIP LOCKED LIMIT 1"""
    ).fetchone()
    if not row:
        connection.commit()
        return None

    connection.execute(
        """UPDATE document_translations
           SET status='processing',attempts=attempts+1,started_at=now(),
               error='',updated_at=now()
           WHERE source_document_id=?""",
        (row["source_document_id"],),
    )
    connection.commit()
    return row


def process_one(connection, task):
    row = connection.execute(
        """SELECT COALESCE(NULLIF(full_text,''),source_text) AS text
             FROM source_documents WHERE id=?""",
        (task["source_document_id"],),
    ).fetchone()
    text = (row["text"] if row else "") or ""
    if not text.strip():
        raise RuntimeError("Document has no text to translate")

    result, provider, source_language = translate_text(
        text,
        task["language"] or TARGET_LANGUAGE,
    )
    model_value = f"{provider};source={source_language}"
    connection.execute(
        """UPDATE document_translations
           SET status='completed',translated_text=?,model=?,error='',
               completed_at=now(),updated_at=now()
           WHERE source_document_id=?""",
        (result, model_value, task["source_document_id"]),
    )
    connection.commit()


def fail(connection, task, error):
    connection.execute(
        """UPDATE document_translations
           SET status='failed',error=?,completed_at=now(),updated_at=now()
           WHERE source_document_id=?""",
        (str(error)[:4000], task["source_document_id"]),
    )
    connection.commit()


def enqueue(connection, document_id: int, language: str = TARGET_LANGUAGE):
    connection.execute(
        """INSERT INTO document_translations(source_document_id,language,status,model)
           VALUES (?,?,'queued',?)
           ON CONFLICT(source_document_id) DO UPDATE SET
             language=excluded.language,status='queued',translated_text='',
             model=excluded.model,error='',started_at=NULL,completed_at=NULL,updated_at=now()""",
        (document_id, language, PROVIDER),
    )
    connection.commit()


def kick_worker():
    log_dir = ROOT / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "translator.log").open(
        "a", encoding="utf-8", buffering=1
    )
    kwargs = {
        "cwd": ROOT,
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "env": {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "translation_worker.py"),
            "--drain",
        ],
        **kwargs,
    )
    log.close()

