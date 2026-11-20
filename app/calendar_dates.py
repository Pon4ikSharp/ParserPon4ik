"""Conservative date extraction for legacy calendar rows.

The legacy collector stored the date in Ukrainian event text while the
PostgreSQL migration intentionally left structured dates empty.  This module
fills only empty dates and records every change as a calendar revision.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
MONTHS = {
    "січня": 1, "січень": 1, "січні": 1,
    "лютого": 2, "лютий": 2, "лютому": 2,
    "березня": 3, "березень": 3, "березні": 3,
    "квітня": 4, "квітень": 4, "квітні": 4,
    "травня": 5, "травень": 5, "травні": 5,
    "червня": 6, "червень": 6, "червні": 6,
    "липня": 7, "липень": 7, "липні": 7,
    "серпня": 8, "серпень": 8, "серпні": 8,
    "вересня": 9, "вересень": 9, "вересні": 9,
    "жовтня": 10, "жовтень": 10, "жовтні": 10,
    "листопада": 11, "листопад": 11, "листопаді": 11,
    "грудня": 12, "грудень": 12, "грудні": 12,
}
MONTH_PATTERN = "|".join(MONTHS)
RANGE_SAME = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s*[–—-]\s*(\d{{1,2}})\s+({MONTH_PATTERN})(?:\s+(20\d{{2}})(?:\s+року)?)?",
    re.I,
)
RANGE_CROSS = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s+({MONTH_PATTERN})\s*(?:[–—-]|та)\s*(\d{{1,2}})\s+({MONTH_PATTERN})(?:\s+(20\d{{2}})(?:\s+року)?)?",
    re.I,
)
SINGLE_DAY = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s+({MONTH_PATTERN})(?:\s+(20\d{{2}})(?:\s+року)?)?",
    re.I,
)
MONTH_ONLY = re.compile(
    rf"\b(на початку|наприкінці|у кінці|до кінця|у середині|в середині|у|в)?\s*({MONTH_PATTERN})(?:\s+(20\d{{2}})(?:\s+року)?)?\b",
    re.I,
)


def _observed(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KYIV)


def _year(text: str, explicit: str | None, month: int, observed: datetime) -> int:
    if explicit:
        return int(explicit)
    key_year = re.search(r"\b(20\d{2})\b", text)
    if key_year:
        return int(key_year.group(1))
    # Calendar candidates describe future events.  A month far behind the
    # observation month therefore belongs to the next year.
    return observed.year + (1 if month < observed.month - 4 else 0)


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _iso(day: date, clock: time | None = None) -> str:
    value = datetime.combine(day, clock or time.min, KYIV)
    return value.astimezone(timezone.utc).isoformat()


def _result(start: date, end: date | None, precision: str, confidence: int, label: str, clock=None):
    return {
        "starts_at": _iso(start, clock),
        "ends_at": _iso(end, clock) if end else None,
        "all_day": clock is None,
        "date_precision": precision if clock is None else "exact",
        "confidence": confidence,
        "label": label,
    }


def _future(result, now=None):
    """Keep only windows that have not ended in Kyiv."""
    if not result:
        return None
    now = _observed(now)
    boundary = result.get("ends_at") or result.get("starts_at")
    if not boundary:
        return result
    parsed = datetime.fromisoformat(str(boundary).replace("Z", "+00:00")).astimezone(KYIV)
    if result.get("all_day"):
        return result if parsed.date() >= now.date() else None
    return result if parsed >= now else None


def extract_event_window(text: str, observed_at=None):
    """Return a conservative structured date or ``None``.

    Exact calendar words win over relative expressions.  Relative expressions
    are deliberately marked approximate and remain tentative in the database.
    """
    raw = " ".join((text or "").replace("#подія", " ").split())
    lower = raw.lower()
    observed = _observed(observed_at)

    match = RANGE_CROSS.search(lower)
    if match:
        d1, m1, d2, m2, explicit = match.groups()
        m1, m2 = MONTHS[m1.lower()], MONTHS[m2.lower()]
        end_year = _year(lower, explicit, m2, observed)
        start_year = end_year - (1 if m1 > m2 else 0)
        start, end = _valid(start_year, m1, int(d1)), _valid(end_year, m2, int(d2))
        if start and end and start <= end:
            return _result(start, end, "range", 82, match.group(0))

    match = RANGE_SAME.search(lower)
    if match:
        d1, d2, month_name, explicit = match.groups()
        month = MONTHS[month_name.lower()]
        year = _year(lower, explicit, month, observed)
        start, end = _valid(year, month, int(d1)), _valid(year, month, int(d2))
        if start and end and start <= end:
            return _result(start, end, "range", 85, match.group(0))

    match = SINGLE_DAY.search(lower)
    if match:
        day, month_name, explicit = match.groups()
        month = MONTHS[month_name.lower()]
        year = _year(lower, explicit, month, observed)
        parsed = _valid(year, month, int(day))
        if parsed:
            clock_match = re.search(r"(?:о|об)\s+(\d{1,2})[:.]([0-5]\d)", lower)
            clock = time(int(clock_match.group(1)), int(clock_match.group(2))) if clock_match else None
            return _result(parsed, None, "day", 88, match.group(0), clock)

    match = MONTH_ONLY.search(lower)
    if match:
        qualifier, month_name, explicit = match.groups()
        month = MONTHS[month_name.lower()]
        year = _year(lower, explicit, month, observed)
        last = calendar.monthrange(year, month)[1]
        qualifier = (qualifier or "").lower()
        if "початку" in qualifier:
            return _result(date(year, month, 1), date(year, month, 10), "approximate", 60, match.group(0))
        if "середині" in qualifier:
            return _result(date(year, month, 11), date(year, month, 20), "approximate", 60, match.group(0))
        if "кін" in qualifier:
            return _result(date(year, month, 21), date(year, month, last), "approximate", 58, match.group(0))
        return _result(date(year, month, 1), date(year, month, last), "month", 65, match.group(0))

    monday = observed.date() - timedelta(days=observed.weekday())
    if re.search(r"\bцього тижня\b", lower):
        return _result(monday, monday + timedelta(days=6), "approximate", 48, "цього тижня")
    if re.search(r"\bнаступн(?:ого|ий) тижн", lower):
        start = monday + timedelta(days=7)
        return _result(start, start + timedelta(days=6), "approximate", 48, "наступного тижня")
    if re.search(r"\bза два тижні\b", lower):
        target = observed.date() + timedelta(days=14)
        return _result(target - timedelta(days=2), target + timedelta(days=2), "approximate", 42, "за два тижні")
    if re.search(r"\bнайближчими? днями\b", lower):
        start = observed.date() + timedelta(days=1)
        return _result(start, start + timedelta(days=6), "approximate", 38, "найближчими днями")
    if re.search(r"\bнайближчим часом\b", lower):
        start = observed.date()
        return _result(start, start + timedelta(days=14), "approximate", 32, "найближчим часом")
    season = re.search(r"\b(весн(?:а|ою)|літ(?:о|ом)|осін(?:ь|ню)|зим(?:а|ою))\s+(20\d{2})(?:\s+року)?\b", lower)
    if season:
        name, year_text = season.groups()
        year = int(year_text)
        if name.startswith("весн"):
            start, end = date(year, 3, 1), date(year, 5, 31)
        elif name.startswith("літ"):
            start, end = date(year, 6, 1), date(year, 8, 31)
        elif name.startswith("осін"):
            start, end = date(year, 9, 1), date(year, 11, 30)
        else:
            start, end = date(year, 12, 1), date(year + 1, 2, 28 + calendar.isleap(year + 1))
        return _result(start, end, "approximate", 45, season.group(0))
    return None


def preview_unknown_dates(connection, limit=500):
    rows = connection.execute(
        """SELECT id,canonical_key,title,summary,first_seen_at
             FROM calendar_events
            WHERE starts_at IS NULL AND date_precision='unknown'
            ORDER BY first_seen_at,id LIMIT ?""",
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        parsed = extract_event_window(
            " ".join([item.get("title", ""), item.get("summary", ""), item.get("canonical_key", "")]),
            item.get("first_seen_at"),
        )
        if parsed:
            parsed = _future(parsed)
            if parsed:
                result.append({**item, **parsed})
    return result


def apply_unknown_dates(connection):
    previews = preview_unknown_dates(connection)
    for item in previews:
        before = connection.execute("SELECT * FROM calendar_events WHERE id=?", (item["id"],)).fetchone()
        confidence = max(int(before["confidence"] or 0), item["confidence"])
        connection.execute(
            """UPDATE calendar_events
                  SET starts_at=?,ends_at=?,all_day=?,date_precision=?,confidence=?,updated_at=now()
                WHERE id=? AND starts_at IS NULL AND date_precision='unknown'""",
            (item["starts_at"], item["ends_at"], item["all_day"], item["date_precision"], confidence, item["id"]),
        )
        revision = connection.execute(
            "SELECT COALESCE(max(revision_no),0)+1 FROM calendar_event_revisions WHERE event_id=?",
            (item["id"],),
        ).fetchone()[0]
        snapshot = connection.execute("SELECT * FROM calendar_events WHERE id=?", (item["id"],)).fetchone()
        connection.execute(
            """INSERT INTO calendar_event_revisions
                 (event_id,revision_no,snapshot,changed_fields,reason,model)
               VALUES (?,?,?::jsonb,?::jsonb,?,?)""",
            (
                item["id"], revision,
                json.dumps(dict(snapshot), ensure_ascii=False, default=str),
                json.dumps(["starts_at", "ends_at", "all_day", "date_precision", "confidence"]),
                f"Дату розпізнано зі старого тексту: {item['label']}", "legacy-date-parser",
            ),
        )
    connection.commit()
    return previews
