from __future__ import annotations

import re
import unicodedata

HASHTAG_RE = re.compile(r"(?<!\w)#[\w\-]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")
COLON_LABEL_RE = re.compile(
    r"^([\wА-Яа-яЁёІіЇїЄє0-9][^:\n]{0,58}?)\s*:\s*(?:\S|$)", re.UNICODE
)
LEADING_DECOR_RE = re.compile(r"^[^\w#А-Яа-яЁёІіЇїЄє0-9]{0,10}", re.UNICODE)

LABEL_STOP = {
    "http", "https", "telegram", "подписаться", "підписатися",
    "источник", "джерело", "ссылка", "посилання", "фото", "видео",
}


def normalize_marker(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return SPACE_RE.sub(" ", value).strip(" \t\r\n.,;!?—–-()[]{}<>|/\\*_=+~'\"")


def _valid_label(value: str) -> bool:
    norm = normalize_marker(value)
    if not norm or len(norm) < 2 or len(norm) > 60:
        return False
    words = norm.split()
    if len(words) > 6:
        return False
    if norm in LABEL_STOP or any(norm.startswith(x + " ") for x in LABEL_STOP):
        return False
    if norm.replace(".", "").replace(":", "").isdigit():
        return False
    if norm.startswith(("http ", "https ", "t.me ")):
        return False
    return True


def extract_markers(text: str):
    raw = text or ""
    out = []
    seen = set()
    for index, tag in enumerate(HASHTAG_RE.findall(raw)):
        norm = normalize_marker(tag)
        key = ("hashtag", norm)
        if norm and key not in seen:
            seen.add(key)
            out.append(("hashtag", tag, norm, index))

    label_index = 0
    for line in raw.splitlines()[:24]:
        candidate = LEADING_DECOR_RE.sub("", line.strip())
        if not candidate or candidate.startswith("#"):
            continue
        match = COLON_LABEL_RE.match(candidate)
        if not match:
            continue
        label = match.group(1).strip()
        if not _valid_label(label):
            continue
        norm = normalize_marker(label)
        key = ("prefix_label", norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(("prefix_label", label, norm, label_index))
        label_index += 1
    return tuple(out)


def store_document_markers(connection, row, text: str | None = None) -> int:
    if str(row["source_kind"] or "").lower() != "telegram":
        return 0
    body = text if text is not None else (row["full_text"] or row["source_text"] or row["feed_text"] or "")
    markers = extract_markers(body)
    for marker_type, marker, normalized, index in markers:
        connection.execute(
            """INSERT INTO telegram_document_markers_v1(
                   source_document_id,source_id,channel_title,marker_type,marker,
                   normalized_marker,marker_index,extracted_at)
               VALUES(?,?,?,?,?,?,?,now()) ON CONFLICT DO NOTHING""",
            (int(row["id"]), row["source_id"], row["source_title"] or "",
             marker_type, marker, normalized, int(index)),
        )
    return len(markers)
