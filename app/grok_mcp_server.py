"""Scoped MCP gateway for Grok Bot -> ParserPon4ik.

Reads pending candidates and writes only to isolated grok_*_v1 analysis tables.
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app import db
from app.grok_mcp_analysis_tools import register_analysis_tools

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("GROK_MCP_PORT", "3001"))

mcp = MCPServer(
    name="ParserPon4ik Grok Analysis v1",
    description="Scoped analysis connector for pending information-influence candidates.",
    instructions=(
        "Read candidates, save normalized semantic analysis, and manage only "
        "the isolated grok_*_v1 story layer. Raw documents, collector state, "
        "and legacy analysis tables are outside this connector's write scope."
    ),
)

def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


@mcp.tool()
def health() -> dict:
    """Return gateway health and queue counts. Does not mutate the database."""
    c = db.connect(readonly=True)
    try:
        rows = c.execute(
            "SELECT status, count(*) AS n "
            "FROM primary_info_queue_v1 GROUP BY status ORDER BY status"
        ).fetchall()
        analysis = c.execute(
            """
            SELECT count(*)::int AS analyzed,
                   count(*) FILTER (WHERE relevant)::int AS relevant,
                   count(*) FILTER (WHERE keep_for_analysis)::int AS kept
              FROM grok_document_analysis_v1
            """
        ).fetchone()
        stories = c.execute(
            "SELECT count(*)::int AS n FROM grok_stories_v1"
        ).fetchone()
        return {
            "ok": True,
            "service": "ParserPon4ik Grok MCP",
            "mode": "analysis_v1_scoped_write",
            "queue": {str(r["status"]): int(r["n"]) for r in rows},
            "analysis_v1": dict(analysis),
            "stories_v1": int(stories["n"]),
        }
    finally:
        c.close()


@mcp.tool()
def get_pending_documents(limit: int = 5, max_chars: int = 2500) -> dict:
    """Get newest pending canonical candidates. Limit is capped at 10."""
    limit = _bounded(limit, 1, 10)
    max_chars = _bounded(max_chars, 500, 4000)
    c = db.connect(readonly=True)
    try:
        rows = c.execute(
            """
            SELECT q.source_document_id AS id, q.cluster_id, q.status,
                   q.classifier_version, d.source_kind,
                   d.source_title AS source, d.source_url AS url,
                   d.published_at, d.created_at,
                   COALESCE(s.groups,'[]'::jsonb) AS source_groups,
                   COALESCE(s.audiences,'[]'::jsonb) AS audiences,
                   COALESCE(s.country_spaces,'[]'::jsonb) AS country_spaces,
                   LEFT(COALESCE(NULLIF(d.full_text,''),d.source_text,''), ?) AS text
              FROM primary_info_queue_v1 q
              JOIN source_documents d ON d.id=q.source_document_id
         LEFT JOIN sources s ON s.id=d.source_id
             WHERE q.status='pending'
             ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz) DESC NULLS LAST,
                      d.id DESC
             LIMIT ?
            """,
            (max_chars, limit),
        ).fetchall()
        return {"count": len(rows), "items": [dict(r) for r in rows]}
    finally:
        c.close()


@mcp.tool()
def get_document(document_id: int, max_chars: int = 6000) -> dict:
    """Read one pending document by id. Full text is capped at 12000 chars."""
    max_chars = _bounded(max_chars, 500, 12000)
    c = db.connect(readonly=True)
    try:
        row = c.execute(
            """
            SELECT q.source_document_id AS id, q.cluster_id, q.status,
                   q.classifier_version, q.reason,
                   d.source_kind, d.source_title AS source, d.source_url AS url,
                   d.published_at, d.created_at, d.content_status,
                   COALESCE(s.groups,'[]'::jsonb) AS source_groups,
                   COALESCE(s.audiences,'[]'::jsonb) AS audiences,
                   COALESCE(s.country_spaces,'[]'::jsonb) AS country_spaces,
                   LEFT(COALESCE(NULLIF(d.full_text,''),d.source_text,''), ?) AS text
              FROM primary_info_queue_v1 q
              JOIN source_documents d ON d.id=q.source_document_id
         LEFT JOIN sources s ON s.id=d.source_id
             WHERE q.source_document_id=? AND q.status='pending'
            """,
            (max_chars, int(document_id)),
        ).fetchone()
        if not row:
            return {"found": False, "id": int(document_id)}
        return {"found": True, "document": dict(row)}
    finally:
        c.close()


register_analysis_tools(mcp)

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:3001",
        "localhost:3001",
        "46.232.251.108",
        "46.232.251.108:18800",
    ],
    allowed_origins=["https://46.232.251.108:18800"],
)

app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    transport_security=transport_security,
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=PORT,
        log_level="info",
        access_log=False,
    )
