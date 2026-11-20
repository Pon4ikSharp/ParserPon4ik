"""Zero-cost prefilter and conservative duplicate detection for primary analysis.

The module intentionally avoids embeddings/LLM calls.  It only suppresses an LLM
call when the content is clearly junk, an exact normalized repeat, or a very close
textual retransmission.  Every source document remains in PostgreSQL.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FAST_PREFIX = "Швидкий фільтр:"
EXACT_PREFIX = "Дублікат exact:"
NEAR_PREFIX = "Дублікат near:"
ERROR_DROP_PREFIX = "Автоматично вилучено з черги після помилки"

_TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "yclid", "mc_cid", "mc_eid", "igshid",
    "vero_conv", "vero_id", "mkt_tok", "spm", "ref_src", "ref_url",
    "traffic_source", "cmpid",
}

# A short item containing one of these markers is not discarded merely because
# it has little text.  This keeps terse statements by important actors/topics.
_PROTECTED_MARKERS = (
    "україн", "росі", "росс", "київ", "киев", "кремл", "зеленськ", "зеленск",
    "путін", "путин", "лавров", "захаров", "пєсков", "песков", "медвед",
    "нато", "nato", "єс", "евросоюз", "eu ", "сша", "usa", "трамп",
    "військ", "военн", "зсу", "всу", "мобіліз", "мобилиз", "санкц",
    "ракета", "дрон", "бпла", "удар", "обстр", "фронт", "переговор",
    "окуп", "крим", "донец", "луган", "запор", "херсон", "молд", "польщ",
)

_SYSTEM_PATTERNS = (
    r"^channel (?:was )?created$", r"^group (?:was )?created$",
    r"^pinned (?:a )?message$", r"^message (?:was )?deleted$",
    r"^joined the channel$", r"^left the channel$",
    r"^канал (?:создан|створено)$", r"^группа создана$",
    r"^повідомлення (?:видалено|закріплено)$", r"^сообщение (?:удалено|закреплено)$",
)

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", re.I)
# Unicode-aware tokenization: catches Arabic and other scripts, not only Latin/Cyrillic.
# Excludes underscore while keeping letters/digits from all Unicode alphabets.
_WORD_RE = re.compile(r"[^\W_]{2,}", re.U)


def canonicalize_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in _TRACKING_KEYS:
            continue
        query.append((key, val))
    # Fragments are almost always navigation/tracking and do not identify a news item.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query, doseq=True), ""))


def _canonicalize_urls_in_text(text: str) -> str:
    return _URL_RE.sub(lambda m: canonicalize_url(m.group(0).rstrip(".,;:!?)]}")), text)


def normalize_text(text: str) -> str:
    value = html.unescape(text or "")
    value = unicodedata.normalize("NFKC", value)
    value = _canonicalize_urls_in_text(value)
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = value.lower()
    value = value.translate(str.maketrans({
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
        "’": "'", "‘": "'", "–": "-", "—": "-", "−": "-",
        "\u00a0": " ",
    }))
    # Remove only known distribution boilerplate, not ordinary article sentences.
    lines = []
    for raw in value.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if re.fullmatch(r"(?:подписаться|підписатися|subscribe)(?:\s+на\s+канал)?[.!]*", line):
            continue
        if re.fullmatch(r"(?:читать|читати|read)\s+(?:далее|далі|more)[.!]*", line):
            continue
        lines.append(line)
    value = " ".join(lines)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalized_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8", errors="ignore")).hexdigest()


def rss_ingest_normalized_hash(text: str) -> str:
    """Legacy text fingerprint retained only for diagnostics/clustering.

    v6.3 SAFE never uses this value to suppress RSS document creation.
    """
    norm = normalize_text(text)
    if len(norm) < 120 or len(meaningful_tokens(norm)) < 12:
        return ""
    return hashlib.sha256(norm.encode("utf-8", errors="ignore")).hexdigest()


def meaningful_tokens(text: str) -> list[str]:
    return [token.lower() for token in _WORD_RE.findall(normalize_text(text))]


def _protected(low: str) -> bool:
    padded = low + " "
    return any(marker in padded for marker in _PROTECTED_MARKERS)


def quick_junk_reason(text: str) -> str:
    """Conservative local filter. Returns empty string when Codex should decide."""
    compact = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    if not compact:
        return "порожній матеріал/медіа або частина Telegram-альбому без підпису"
    low = compact.lower().strip()

    if any(re.fullmatch(pattern, low.strip(" .!?:;-—–[]()")) for pattern in _SYSTEM_PATTERNS):
        return "службове повідомлення платформи"

    without_urls = _URL_RE.sub(" ", compact)
    words = meaningful_tokens(without_urls)
    protected = _protected(low)

    # Link/emoji-only and truly tiny captions.  Important named actors/topics are protected.
    if not words and _URL_RE.search(compact):
        return "лише посилання/службові символи без змістовного тексту"
    # Do not suppress two-word headlines: terse headlines such as "Освобождение Зоревки",
    # "Situation à Zaporijia" or "Orbán-Wadephul" may be analytically important.
    # Only a truly one-token caption is locally discarded.
    if len(words) <= 1 and len(without_urls.strip()) < 90 and not protected:
        return "0–1 змістовне слово без достатнього інформаційного контексту"

    media_only = {"фото", "відео", "video", "photo", "image", "кружок", "сторис", "reel", "shorts"}
    stripped = low.strip(" .!?:;-—–[]()#")
    if stripped in media_only:
        return "медіа/частина альбому без змістовного тексту"

    hard_ad = (
        "на правах рекламы", "на правах реклами", "рекламный блок", "рекламний блок",
        "advertisement", "sponsored post", "партнерский материал", "партнерський матеріал",
    )
    if any(marker in low for marker in hard_ad):
        return "очевидна реклама/партнерський матеріал"
    if low.startswith(("реклама:", "реклама ", "advertisement:", "sponsored:")) and not protected:
        return "очевидна реклама"
    commercial = sum(marker in low for marker in (
        "промокод", "скидка", "знижка", "заказать", "замовити", "доставка",
        "купи сейчас", "купи зараз", "акция действует", "акція діє", "cashback", "кэшбэк",
    ))
    if commercial >= 2 and len(compact) < 1400 and not protected:
        return "рекламний шаблон"

    # Mechanical market ticker with no policy/geopolitical context.
    quote_markers = sum(marker in low for marker in (
        "котировки", "котирування", "курс валют", "биржевой курс", "біржовий курс",
        "usd/", "eur/", "rub/", "btc/", "brent ", "wti ",
    ))
    if quote_markers and len(compact) < 360 and not protected:
        if re.search(r"\b\d+[.,]?\d*\s*(?:%|₽|руб|грн|usd|eur|долл|€|\$)", low):
            return "коротке біржове/валютне котирування без контексту"

    # Pure weather/sport score snippets only; articles with political/security context are kept.
    if len(compact) < 320 and not protected:
        weather = any(x in low for x in ("прогноз погоды", "прогноз погоди", "температура воздуха", "температура повітря"))
        sport = any(x in low for x in ("итоговый счет", "підсумковий рахунок", "матч завершился", "матч завершився"))
        if weather:
            return "короткий прогноз погоди без релевантного контексту"
        if sport and re.search(r"\b\d{1,2}\s*[:—-]\s*\d{1,2}\b", low):
            return "короткий спортивний результат без релевантного контексту"

    return ""


def _shingles(tokens: list[str], width: int = 3) -> set[tuple[str, ...]]:
    if len(tokens) < width:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + width]) for i in range(len(tokens) - width + 1)}


def near_similarity(left: str, right: str) -> float:
    lt = meaningful_tokens(left)
    rt = meaningful_tokens(right)
    if len(lt) < 30 or len(rt) < 30:
        return 0.0
    ratio = min(len(lt), len(rt)) / max(len(lt), len(rt))
    if ratio < 0.78:
        return 0.0

    # A materially different lead can change propaganda framing even when the
    # syndicated body is copied. Keep those for Codex instead of inheriting.
    lead_left, lead_right = set(lt[:60]), set(rt[:60])
    lead_overlap = len(lead_left & lead_right) / max(1, len(lead_left | lead_right))
    if lead_overlap < 0.38:
        return 0.0

    # Several new/different figures often mean an updated story, not a repeat.
    nums_left = {x for x in lt if any(ch.isdigit() for ch in x)}
    nums_right = {x for x in rt if any(ch.isdigit() for ch in x)}
    if len(nums_left ^ nums_right) > 2:
        return 0.0

    ls, rs = _shingles(lt, 3), _shingles(rt, 3)
    if not ls or not rs:
        return 0.0
    intersection = len(ls & rs)
    union = len(ls | rs)
    jaccard = intersection / union if union else 0.0
    containment = intersection / min(len(ls), len(rs))
    token_left, token_right = set(lt), set(rt)
    token_j = len(token_left & token_right) / max(1, len(token_left | token_right))
    # Containment helps syndicated copies with a short publisher-specific lead,
    # while the length/lead/number guards above protect meaningful updates.
    return max(jaccard, containment * 0.96) * 0.82 + token_j * 0.18


def _source_text(row) -> str:
    return ((row.get("full_text") if hasattr(row, "get") else row["full_text"]) or
            (row.get("source_text") if hasattr(row, "get") else row["source_text"]) or "").strip()


def _resolve_root(database, document_id: int) -> int:
    link = database.execute(
        "SELECT canonical_document_id FROM primary_dedup_links WHERE duplicate_document_id=?",
        (int(document_id),),
    ).fetchone()
    database.commit()
    return int(link["canonical_document_id"]) if link else int(document_id)


def _row_value(row, name: str, default=""):
    try:
        value = row[name]
    except Exception:
        try:
            value = row.get(name, default)
        except Exception:
            value = default
    return value if value is not None else default


def _entry_title(row) -> str:
    """Best-effort RSS/web headline from the preserved feed text."""
    raw = str(_row_value(row, "source_text", "") or "")
    for line in raw.splitlines():
        value = re.sub(r"\s+", " ", html.unescape(line)).strip()
        if value:
            return normalize_text(value)[:500]
    return ""


def _title_similarity(left_row, right_row) -> float:
    left = _entry_title(left_row)
    right = _entry_title(right_row)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    lt, rt = set(meaningful_tokens(left)), set(meaningful_tokens(right))
    token_j = len(lt & rt) / max(1, len(lt | rt)) if lt and rt else 0.0
    seq = SequenceMatcher(None, left, right).ratio()
    return max(token_j, seq * 0.9)


def _is_rss_or_web(row) -> bool:
    return str(_row_value(row, "source_kind", "") or "").lower() in {"rss", "web"}


def _exact_basis(row, hydrated_text: str) -> str:
    # RSS feed text preserves the headline + feed description and is safer than
    # hydrated page boilerplate for exact-copy detection.
    if _is_rss_or_web(row):
        preserved = str(_row_value(row, "source_text", "") or "").strip()
        if preserved:
            return preserved
    return hydrated_text


def inflight_exact_key(row, text: str) -> int | None:
    """Stable 31-bit lock key; RSS/web prefers canonical URL over body text."""
    if _is_rss_or_web(row):
        canonical = canonicalize_url(str(_row_value(row, "source_url", "") or ""))
        if canonical:
            raw = "url|" + canonical
        else:
            basis = _exact_basis(row, text)
            title = _entry_title(row)
            if not basis or not title:
                return None
            raw = "feed|" + title + "|" + normalize_text(basis)
    else:
        norm = normalize_text(text)
        if not norm:
            return None
        raw = "text|" + norm
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:8], 16) & 0x7FFFFFFF


def _simhash64(tokens: list[str]) -> int:
    """Small local LSH fingerprint; no network/LLM use."""
    if not tokens:
        return 0
    # Use unique 3-shingles so publisher boilerplate/repetition does not dominate.
    shingles = _shingles(tokens, 3) or {tuple(tokens)}
    vector = [0] * 64
    for shingle in shingles:
        raw = "\x1f".join(shingle).encode("utf-8", errors="ignore")
        value = int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            result |= 1 << bit
    return result


def inflight_near_bucket(row, text: str) -> int | None:
    """Conservative in-flight bucket; never declares a duplicate by itself.

    For RSS/web the headline fingerprint is part of the bucket so generic page
    boilerplate from unrelated stories cannot serialize each other.
    """
    tokens = meaningful_tokens(text)
    if len(tokens) < 30 or len(normalize_text(text)) < 220:
        return None
    sim = _simhash64(tokens)
    if _is_rss_or_web(row):
        title = _entry_title(row)
        title_tokens = meaningful_tokens(title)
        if len(title_tokens) < 3:
            return None
        title_digest = hashlib.blake2b(title.encode("utf-8", errors="ignore"), digest_size=2).digest()
        title_bucket = int.from_bytes(title_digest, "big") & 0xFF
        body_bucket = (sim >> 56) & 0xFF
        return (title_bucket << 8) | body_bucket
    return (sim >> 48) & 0xFFFF


@dataclass(frozen=True)
class DuplicateMatch:
    canonical_document_id: int
    duplicate_type: str
    similarity: float
    normalized_hash: str


def find_duplicate(database, row, text: str) -> DuplicateMatch | None:
    """Find only a safely reusable, already-completed primary analysis."""
    document_id = int(row["id"])
    norm = normalize_text(text)
    if not norm:
        return None
    exact_basis = _exact_basis(row, text)
    exact_norm = normalize_text(exact_basis)
    n_hash = hashlib.sha256(exact_norm.encode("utf-8", errors="ignore")).hexdigest()

    # content_hash is the hash of the preserved source_text at ingest time. For
    # RSS this includes headline + feed description and avoids hydrated-page
    # boilerplate collisions.
    raw_hash = hashlib.sha256(exact_basis.encode("utf-8", errors="ignore")).hexdigest()
    exact = database.execute(
        """SELECT d.id
             FROM source_documents d
             JOIN info_influence i ON i.source_document_id=d.id
            WHERE d.id<>? AND d.content_hash=?
              AND i.status IN ('accepted','rejected')
              AND COALESCE(i.reason,'') NOT LIKE ?
              AND COALESCE(i.reason,'') NOT LIKE ?
            ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                              NULLIF(d.created_at,'')::timestamptz), d.id
            LIMIT 1""",
        (document_id, raw_hash, FAST_PREFIX + "%", ERROR_DROP_PREFIX + "%"),
    ).fetchone()
    database.commit()
    if exact:
        return DuplicateMatch(_resolve_root(database, int(exact["id"])), "exact", 1.0, n_hash)

    # Compare only with nearby, already classified publications. This is cheap
    # and prevents grouping recurring talking points days apart as one story.
    candidate_hours = max(6, min(72, int(os.getenv("PRIMARY_NEAR_DUP_WINDOW_HOURS", "36"))))
    min_chars = max(1, int(len(text) * 0.70))
    max_chars = max(min_chars + 1, int(len(text) * 1.40) + 200)
    candidates = database.execute(
        """SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,d.source_text,d.full_text,
                  d.published_at,d.created_at,d.content_hash
             FROM source_documents d
             JOIN info_influence i ON i.source_document_id=d.id
            WHERE d.id<>?
              AND i.status IN ('accepted','rejected')
              AND COALESCE(i.reason,'') NOT LIKE ?
              AND COALESCE(i.reason,'') NOT LIKE ?
              AND length(trim(COALESCE(NULLIF(d.full_text,''),d.source_text,''))) BETWEEN ? AND ?
              AND abs(extract(epoch from (
                    COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)
                    - ?::timestamptz
                  ))) <= (? * 3600)
            ORDER BY abs(extract(epoch from (
                    COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)
                    - ?::timestamptz
                  )))
            LIMIT 100""",
        (
            document_id, FAST_PREFIX + "%", ERROR_DROP_PREFIX + "%", min_chars, max_chars,
            row["published_at"] or row["created_at"], candidate_hours,
            row["published_at"] or row["created_at"],
        ),
    ).fetchall()
    database.commit()

    threshold = max(0.80, min(0.98, float(os.getenv("PRIMARY_NEAR_DUP_THRESHOLD", "0.86"))))
    best = None
    for candidate in candidates:
        candidate_text = _source_text(candidate)
        if not candidate_text:
            continue
        candidate_norm = normalize_text(candidate_text)
        if not candidate_norm:
            continue

        candidate_exact_basis = _exact_basis(candidate, candidate_text)
        candidate_exact_norm = normalize_text(candidate_exact_basis)
        candidate_hash = hashlib.sha256(candidate_exact_norm.encode("utf-8", errors="ignore")).hexdigest()
        title_similarity = _title_similarity(row, candidate) if (_is_rss_or_web(row) or _is_rss_or_web(candidate)) else 1.0
        same_source = int(_row_value(row, "source_id", 0) or 0) == int(_row_value(candidate, "source_id", -1) or -1)
        same_canonical_url = bool(
            same_source
            and canonicalize_url(str(_row_value(row, "source_url", "") or ""))
            and canonicalize_url(str(_row_value(row, "source_url", "") or ""))
                == canonicalize_url(str(_row_value(candidate, "source_url", "") or ""))
        )
        if candidate_hash == n_hash and (not (_is_rss_or_web(row) or _is_rss_or_web(candidate)) or title_similarity >= 0.72 or same_canonical_url):
            return DuplicateMatch(_resolve_root(database, int(candidate["id"])), "exact", 1.0, n_hash)

        # Near-dedup is intentionally disabled for short items. For RSS/web we
        # additionally require headline similarity so generic templates from
        # unrelated stories can never inherit one another's analysis.
        if min(len(norm), len(candidate_norm)) < 220:
            continue
        if (_is_rss_or_web(row) or _is_rss_or_web(candidate)) and title_similarity < 0.45:
            continue
        score = near_similarity(norm, candidate_norm)
        if score >= threshold and (best is None or score > best.similarity):
            best = DuplicateMatch(_resolve_root(database, int(candidate["id"])), "near", score, n_hash)
    return best


def inherited_analysis(database, canonical_document_id: int) -> tuple[dict, str, str] | None:
    row = database.execute(
        """SELECT * FROM info_influence
            WHERE source_document_id=? AND status IN ('accepted','rejected')
            ORDER BY id DESC LIMIT 1""",
        (int(canonical_document_id),),
    ).fetchone()
    if not row:
        return None
    reason = row["reason"] or ""
    if reason.startswith(FAST_PREFIX) or reason.startswith(ERROR_DROP_PREFIX):
        return None
    def arr(name):
        try:
            value = json.loads(row[name] or "[]")
            return value if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []
    analysis = {
        "role": row["role"] or "not_relevant",
        "narrative_title": row["narrative_title"] or "",
        "summary": row["summary"] or "",
        "target_entities": arr("target_entities"),
        "techniques": arr("techniques"),
        "claims": arr("claims"),
        "strategic_objectives": arr("strategic_objectives"),
        "audiences": arr("audiences"),
        "actors": arr("actors"),
        "information_triggers": arr("information_triggers"),
        "future_relevance": row["future_relevance"] or "",
        "confidence": int(row["confidence"] or 0),
        "narrative_key": row["narrative_key"] or "",
    }
    return analysis, row["status"], reason


def register_link(database, *, duplicate_document_id: int, canonical_document_id: int,
                  duplicate_type: str, similarity: float, n_hash: str) -> None:
    root = _resolve_root(database, canonical_document_id)
    database.execute(
        """INSERT INTO primary_dedup_links(
               duplicate_document_id,canonical_document_id,cluster_id,duplicate_type,
               similarity,normalized_hash,created_at
           ) VALUES (?,?,?,?,?,?,now())
           ON CONFLICT(duplicate_document_id) DO UPDATE SET
               canonical_document_id=excluded.canonical_document_id,
               cluster_id=excluded.cluster_id,
               duplicate_type=excluded.duplicate_type,
               similarity=excluded.similarity,
               normalized_hash=excluded.normalized_hash""",
        (int(duplicate_document_id), int(root), int(root), duplicate_type, float(similarity), n_hash),
    )
    database.commit()
