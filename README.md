# chessmaster

An MCP server that connects Claude to your Lichess account for chess analysis and stats.

## Example prompts

- "What openings am I worst at as Black? Look at my last 200 rated blitz games"
- "Where do I lose most of my games — opening, middlegame, or endgame? Analyse my last 20 games"
- "Show me my blitz rating history — am I improving?"
- "Fetch and analyze my last game, tell me my biggest mistakes"
- "What does the Lichess explorer say about my win rate after 1.e4 as White?"

## Tools

| Tool | Description |
|------|-------------|
| `get_recent_games` | Fetch recent games with optional filters (time control, color, rated) |
| `get_performance_stats` | Rating, W/D/L counts, and progress across all time controls |
| `get_rating_history` | Full rating history per variant |
| `get_opening_stats` | Win/draw/loss rates by opening via the Lichess explorer |
| `get_opening_performance` | Opening stats aggregated from your recent games |
| `get_puzzle_activity` | Recent puzzle attempts |
| `analyze_game` | Analyse a PGN with local Stockfish (blunders, mistakes, accuracy) |
| `fetch_and_analyze_game` | Fetch a game by ID and analyze it in one call |
| `open_game` | Open a game in the browser |

## Setup

**Dependencies**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install stockfish  # macOS
```

**Lichess token** (optional — raises rate limit from 20 → 60 req/s)

1. Create a token at `https://lichess.org/account/oauth/token` — no scopes needed (game export is a public endpoint).
2. Store it in 1Password:
   ```bash
   op item create --category=login --title="Lichess API" --vault=Personal token=<paste-token-here>
   ```
3. Launch the server via `op run` (see `.mcp.json`) — it resolves the `op://Personal/Lichess API/token` reference and injects `LICHESS_TOKEN` at startup.

**Running**

```bash
# stdio mode (for MCP clients)
python server.py

# Interactive tool inspector
fastmcp dev server.py
```

## MCP client config

Add to your MCP client config (e.g. `.mcp.json`):

```json
{
  "mcpServers": {
    "chessmaster": {
      "command": ".venv/bin/python",
      "args": ["server.py"]
    }
  }
}
```
