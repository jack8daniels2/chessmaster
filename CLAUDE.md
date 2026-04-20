# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
# stdio mode (standard for MCP clients)
python server.py

# Inspect available tools interactively
fastmcp dev server.py
```

## Auth

`LICHESS_TOKEN` is injected at runtime via 1Password CLI — no `.env` file needed. The token is stored in 1Password at `op://Personal/Lichess API/token`.

To create the PAT: `https://lichess.org/account/oauth/token` — no scopes needed. Game export is a public endpoint; the token exists only to raise the rate limit (20 → 60 games/sec for your own account).

To store it: `op item create --category=login --title="Lichess API" --vault=Personal token=<paste-token-here>`

The MCP server is launched via `op run` (see `.mcp.json`), which resolves the `op://` reference before spawning the process.

## Architecture

This is a single-file FastMCP server (`server.py`) that wraps the [Lichess API](https://lichess.org/api). The MCP instance (`mcp = FastMCP("chessmaster")`) is the central object — tools are registered by decorating async functions with `@mcp.tool()`.

**Lichess API notes:**
- Games endpoint returns NDJSON (`application/x-ndjson`), parsed line-by-line into a list of dicts.
- `evals` only appear in games that were computer-analyzed on Lichess; the field is absent otherwise.
- Rate limits: ~20 req/s unauthenticated, higher with a token.

## Adding new tools

Follow the pattern in `get_recent_games`: one `async def` per tool, decorated with `@mcp.tool()`, using `httpx.AsyncClient` for all HTTP calls. Keep the `import json` at the top level when adding more tools.
