"""No-AI article hydration primitives for Processing Engine v2.

Hydration fetches public article pages and extracts full text. It does not call
an AI model and, by itself, does not write to source_documents.

The live collector does not import this module.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv

from app import articles

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {"extracted", "partial", "paywall", "blocked"}
RETRYABLE_STATUSES = {"error"}


def parse_domains(value: str | None) -> tuple[str, ...]:
    raw = (value or "").strip().strip('"').strip("'")
    domains = []
    for item in raw.split(","):
        domain = item.strip().lower().lstrip(".")
        if domain:
            domains.append(domain)
    return tuple(dict.fromkeys(domains))


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def host_matches_domains(url: str, domains: tuple[str, ...]) -> bool:
    host = hostname(url)
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def configured_proxy() -> tuple[str | None, tuple[str, ...]]:
    """Return proxy URL and the domains allowed to use it.

    Credentials are read from .env but never logged or returned separately.
    If PROXY_ENABLED is false, proxy_url is None.
    """
    load_dotenv(ROOT / ".env")
    domains = parse_domains(os.getenv("RSS_PROXY_DOMAINS", ""))
    enabled = os.getenv("PROXY_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None, domains

    proxy_type = os.getenv("PROXY_TYPE", "http").strip().lower()
    proxy_type = {
        "http": "http",
        "https": "http",
        "socks": "socks5",
        "socks5": "socks5",
        "socks5h": "socks5",
    }.get(proxy_type, "")
    if not proxy_type:
        raise RuntimeError("PROXY_TYPE must be http or socks5")

    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()
    if not all((host, port, username, password)):
        raise RuntimeError("Proxy is enabled but proxy configuration is incomplete")
    try:
        port_number = int(port)
    except ValueError as error:
        raise RuntimeError("PROXY_PORT must be numeric") from error
    if not 1 <= port_number <= 65535:
        raise RuntimeError("PROXY_PORT is out of range")

    credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{proxy_type}://{credentials}{host}:{port_number}", domains


def proxy_for_url(url: str, proxy_url: str | None,
                  proxy_domains: tuple[str, ...]) -> str | None:
    if not proxy_url:
        return None
    # If a domain allow-list is configured, never send other publishers
    # through the proxy. An empty allow-list preserves the legacy "proxy all"
    # behavior for installations that intentionally use it.
    if proxy_domains and not host_matches_domains(url, proxy_domains):
        return None
    return proxy_url


def _exception_status(error: Exception) -> tuple[str, int | None]:
    """Map HTTP authorization/paywall-style failures to terminal blocked."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code in {401, 402, 403}:
        return "blocked", status_code
    return "error", status_code if isinstance(status_code, int) else None


def fetch_document(document: dict, *,
                   proxy_url: str | None = None,
                   proxy_domains: tuple[str, ...] = (),
                   timeout: float = 40.0) -> dict:
    """Fetch one RSS/web document without writing to PostgreSQL."""
    source_kind = (document.get("source_kind") or "").lower()
    url = (document.get("source_url") or "").strip()
    feed_text = (document.get("feed_text") or document.get("source_text") or "").strip()

    if source_kind not in {"rss", "web"}:
        raise ValueError("Hydration is only valid for rss/web documents")
    if not url:
        raise ValueError("Document has no source_url")

    selected_proxy = proxy_for_url(url, proxy_url, proxy_domains)
    started = time.perf_counter()
    try:
        data = articles.fetch(url, feed_text=feed_text, proxy=selected_proxy, timeout=timeout)
    except Exception as error:
        failure_status, status_code = _exception_status(error)
        metadata = {}
        if status_code is not None:
            metadata["http_status"] = status_code
        data = {
            "html": "",
            "text": "",
            "status": failure_status,
            "metadata": metadata,
            "error": f"{type(error).__name__}: {str(error)[:500]}",
        }

    duration_ms = int((time.perf_counter() - started) * 1000)
    metadata = dict(data.get("metadata") or {})
    final_url = str(metadata.get("url") or url)
    text = str(data.get("text") or "")
    html = str(data.get("html") or "")
    status = str(data.get("status") or "error")
    if status not in TERMINAL_STATUSES | RETRYABLE_STATUSES:
        status = "error"

    return {
        "status": status,
        "full_text": text,
        "raw_html": html,
        "metadata": metadata,
        "error": str(data.get("error") or ""),
        "requested_url": url,
        "final_url": final_url,
        "used_proxy": bool(selected_proxy),
        "duration_ms": duration_ms,
        "full_text_chars": len(text),
        "raw_html_chars": len(html),
    }


def usable_text(outcome: dict, fallback_text: str = "") -> tuple[str, str]:
    """Choose the text that a later analysis stage should use."""
    full_text = (outcome.get("full_text") or "").strip()
    status = outcome.get("status")
    if status == "extracted" and full_text:
        return "hydration_v2", full_text
    if full_text and len(full_text) >= 300:
        # Preserve potentially useful text from partial/paywall pages while
        # making the weaker provenance explicit.
        return "hydration_v2_partial", full_text
    return "source_text", (fallback_text or "").strip()


def should_retry(outcome: dict) -> bool:
    return outcome.get("status") in RETRYABLE_STATUSES


def is_terminal(outcome: dict) -> bool:
    return outcome.get("status") in TERMINAL_STATUSES
