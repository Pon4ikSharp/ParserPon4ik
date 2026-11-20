"""ParserPon4ik Grok Analysis v2 MCP server."""
from __future__ import annotations

import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.grok_mcp_v2_tools import register_v2_tools

PORT=int(os.getenv("GROK_MCP_V2_PORT","3002"))

mcp=MCPServer(
    name="ParserPon4ik Analysis v2",
    description="Resumable scoped analysis connector with deterministic story retrieval.",
    instructions=(
        "Use only the resumable run workflow. Writes are limited to grok analysis, "
        "story, audit, run-tracking and story-review tables."
    ),
)

register_v2_tools(mcp)

transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:3002",
        "localhost:3002",
        "46.232.251.108",
        "46.232.251.108:18800",
    ],
    allowed_origins=["https://46.232.251.108:18800"],
)

app=mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    transport_security=transport_security,
)

if __name__=="__main__":
    uvicorn.run(app,host="127.0.0.1",port=PORT,log_level="info",access_log=False)
