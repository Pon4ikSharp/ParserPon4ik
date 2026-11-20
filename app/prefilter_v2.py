"""Deterministic, no-AI prefilter for Processing Engine v2.

The filter is deliberately conservative: ambiguous content passes through.
It removes only high-confidence technical junk and computes fingerprints that
can reduce duplicate AI work without deleting propagation evidence.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, asdict

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
SPACE_RE = re.compile(r"[ \t\u00a0]+")
BLANKS_RE = re.compile(r"\n{3,}")
WORD_RE = re.compile(r"[\w'’\-]+", re.UNICODE)
ALNUM_RE = re.compile(r"[\w\d]", re.UNICODE)
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

BOILERPLATE_PATTERNS = (
    re.compile(r"(?i)підписуй(?:те)?ся|подписывай(?:те)?сь|subscribe"),
    re.compile(r"(?i)наш(?:і|и)\s+(?:канал|чат)|наш\s+телеграм|telegram\s*канал"),
    re.compile(r"(?i)реклам[аиуы]|по\s+вопросам\s+рекламы|advertis"),
    re.compile(r"(?i)бот\s+зворотн(?:ого|ього)\s+зв.?язку|обратн(?:ая|ой)\s+связ"),
    re.compile(r"(?i)надіслати\s+новину|прислать\s+новость|предложить\s+новость"),
    re.compile(r"(?i)став(?:те|ьте)\s+(?:лайк|реакц)|лайк\s+і\s+підпис"),
)

ONLY_LABEL_RE = re.compile(
    r"(?i)^(?:фото|відео|видео|video|photo|стрім|стрим|live|джерело|источник)\s*[:：-]?\s*$"
)


@dataclass(frozen=True)
class PrefilterResult:
    decision: str
    reason: str
    normalized_text: str
    char_count: int
    word_count: int
    url_count: int
    fingerprint64: str
    exact_key: str

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for raw in text.split("\n"):
        line = SPACE_RE.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def words(text: str) -> list[str]:
    return [w.casefold() for w in WORD_RE.findall(text) if any(ch.isalnum() for ch in w)]


def exact_key(text: str) -> str:
    basis = " ".join(words(normalize_text(text)))
    if not basis:
        return ""
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _feature_tokens(text: str) -> list[str]:
    toks = words(text)
    if len(toks) < 3:
        return toks
    return [" ".join(toks[i:i + 3]) for i in range(len(toks) - 2)]


def simhash64(text: str) -> int:
    features = _feature_tokens(normalize_text(text))
    if not features:
        return 0
    vector = [0] * 64
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            vector[bit] += 1 if (value >> bit) & 1 else -1
    result = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            result |= 1 << bit
    return result


def hamming_distance64(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def similarity64(left: int, right: int) -> float:
    return 1.0 - (hamming_distance64(left, right) / 64.0)


def _substantive_after_boilerplate(text: str) -> str:
    cleaned = text
    for pattern in BOILERPLATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = URL_RE.sub(" ", cleaned)
    return SPACE_RE.sub(" ", cleaned).strip(" \n\t—–-:|•·.,!?()[]{}")


def classify(text: str) -> PrefilterResult:
    normalized = normalize_text(text)
    token_list = words(normalized)
    urls = URL_RE.findall(normalized)
    chars = len(normalized)
    word_count = len(token_list)

    decision = "pass"
    reason = "content_candidate"

    if not normalized or not ALNUM_RE.search(normalized):
        decision, reason = "drop", "empty_or_nontext"
    elif ONLY_LABEL_RE.fullmatch(normalized):
        decision, reason = "drop", "media_label_only"
    else:
        without_urls = URL_RE.sub(" ", normalized)
        residual_words = words(without_urls)
        if urls and len(residual_words) <= 1:
            decision, reason = "drop", "link_only"
        elif chars < 12 and word_count <= 2:
            decision, reason = "drop", "too_short_noninformative"
        else:
            matches = sum(bool(p.search(normalized)) for p in BOILERPLATE_PATTERNS)
            residual = _substantive_after_boilerplate(normalized)
            # High-confidence junk only. One subscribe line attached to a real
            # news item must not discard the news.
            if matches >= 2 and len(residual) < 60 and len(normalized) < 260:
                decision, reason = "drop", "technical_or_promotional_only"

    fp = simhash64(normalized)
    return PrefilterResult(
        decision=decision,
        reason=reason,
        normalized_text=normalized,
        char_count=chars,
        word_count=word_count,
        url_count=len(urls),
        fingerprint64=f"{fp:016x}" if fp else "",
        exact_key=exact_key(normalized),
    )


def choose_input(document) -> tuple[str, str]:
    """Return (basis, text) without mutating the document."""
    full_text = (document.get("full_text") or "").strip()
    source_text = (document.get("source_text") or "").strip()
    feed_text = (document.get("feed_text") or "").strip()

    if full_text:
        return "full_text", full_text
    if source_text:
        return "source_text", source_text
    if feed_text:
        return "feed_text", feed_text
    return "source_text", ""


def find_near_duplicates(items: list[dict], *, threshold: float = 0.90) -> dict[int, tuple[int, float]]:
    """Return duplicate_index -> (canonical_index, similarity).

    Intended for bounded batches. Exact duplicates should be handled first.
    Ambiguous short texts are not near-deduplicated because SimHash is unstable
    on tiny inputs.
    """
    result: dict[int, tuple[int, float]] = {}
    candidates = []
    for idx, item in enumerate(items):
        text = item.get("normalized_text") or ""
        if len(words(text)) < 12 or len(text) < 80:
            continue
        fp_hex = item.get("fingerprint64") or ""
        if not fp_hex:
            continue
        candidates.append((idx, int(fp_hex, 16)))

    for pos, (idx, fp) in enumerate(candidates):
        if idx in result:
            continue
        for other_idx, other_fp in candidates[pos + 1:]:
            if other_idx in result:
                continue
            score = similarity64(fp, other_fp)
            if score >= threshold:
                result[other_idx] = (idx, score)
    return result
