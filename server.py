import hashlib
import io
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import chess
import chess.pgn
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from stockfish import Stockfish

load_dotenv()

LICHESS_TOKEN = os.getenv("LICHESS_TOKEN")
LICHESS_BASE = "https://lichess.org"
EXPLORER_BASE = "https://explorer.lichess.ovh"

# Adjust if Stockfish is not on PATH (e.g. "/opt/homebrew/bin/stockfish")
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH", "stockfish")

mcp = FastMCP("chessmaster")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class _Cache:
    def __init__(self, path: Path = Path(__file__).parent / "cache.db"):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str, ttl: float | None = None):
        """Return cached value, or None if missing or expired (ttl=None means永久)."""
        row = self._conn.execute(
            "SELECT value, fetched_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, fetched_at = row
        if ttl is not None and time.time() - fetched_at > ttl:
            return None
        return json.loads(value)

    def set(self, key: str, value) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()


_cache = _Cache()

_TTL_STATS = 3600   # 1 h  — perf stats, rating history, opening explorer, puzzles
_TTL_LIST  = 300    # 5 min — recent games lists (new games may arrive)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(ndjson: bool = True) -> dict:
    h = {"Accept": "application/x-ndjson" if ndjson else "application/json"}
    if LICHESS_TOKEN:
        h["Authorization"] = f"Bearer {LICHESS_TOKEN}"
    return h


def _parse_ndjson(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _sf() -> Stockfish:
    return Stockfish(path=STOCKFISH_PATH)


def _to_white_cp(sf_eval: dict, white_to_move: bool) -> int:
    """Normalise Stockfish eval to white-is-positive centipawns (capped ±3000)."""
    if sf_eval["type"] == "mate":
        raw = 3000 if sf_eval["value"] > 0 else -3000
    else:
        raw = max(-3000, min(3000, sf_eval["value"]))
    # Stockfish reports score from the side-to-move's perspective
    return raw if white_to_move else -raw


def _classify(cp_loss: int) -> str:
    if cp_loss >= 200:
        return "blunder"
    if cp_loss >= 100:
        return "mistake"
    if cp_loss >= 50:
        return "inaccuracy"
    return "good"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_recent_games(
    username: str,
    max_games: int = 10,
    evals: bool = True,
    opening: bool = True,
    analysed_only: bool = False,
    perf_type: str | None = None,
    color: str | None = None,
    rated: bool | None = None,
) -> list[dict]:
    """Fetch recent Lichess games for a user.

    Args:
        username: Lichess username.
        max_games: Number of games to fetch (max 300).
        evals: Include Lichess cloud evaluations (only present if game was analysed).
        opening: Include ECO code and opening name.
        analysed_only: Only return games that have computer analysis.
        perf_type: Filter by time control: bullet, blitz, rapid, classical, correspondence.
        color: Filter by color played: white or black.
        rated: If set, filter rated (True) or casual (False) games only.
    """
    params: dict = {
        "max": min(max_games, 300),
        "evals": str(evals).lower(),
        "opening": str(opening).lower(),
        "clocks": "true",
        "moves": "true",
    }
    if analysed_only:
        params["analysed"] = "true"
    if perf_type:
        params["perfType"] = perf_type
    if color:
        params["color"] = color
    if rated is not None:
        params["rated"] = str(rated).lower()

    params_hash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
    cache_key = f"games:{username.lower()}:{params_hash}"
    cached = _cache.get(cache_key, ttl=_TTL_LIST)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(base_url=LICHESS_BASE, timeout=30) as client:
        r = await client.get(f"/api/games/user/{username}", headers=_headers(), params=params)
        r.raise_for_status()

    result = _parse_ndjson(r.text)
    for game in result:
        if "id" in game:
            game["url"] = f"{LICHESS_BASE}/{game['id']}"
    _cache.set(cache_key, result)
    return result


@mcp.tool()
async def get_performance_stats(username: str) -> dict:
    """Return rating, games played, win/draw/loss counts and recent progress for every
    time control and variant the user has played.

    Args:
        username: Lichess username.
    """
    cache_key = f"perf:{username.lower()}"
    cached = _cache.get(cache_key, ttl=_TTL_STATS)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(base_url=LICHESS_BASE, timeout=15) as client:
        r = await client.get(f"/api/user/{username}", headers=_headers(ndjson=False))
        r.raise_for_status()

    data = r.json()
    result = {
        "username": data.get("username"),
        "title": data.get("title"),
        "playtime_hours": round(data.get("playtime", {}).get("total", 0) / 3600, 1),
        "perfs": data.get("perfs", {}),
        "count": data.get("count", {}),
    }
    _cache.set(cache_key, result)
    return result


@mcp.tool()
async def get_rating_history(username: str) -> list[dict]:
    """Return full rating history across all time controls.

    Each entry contains the variant name and a list of {date, rating} points.

    Args:
        username: Lichess username.
    """
    cache_key = f"rating_history:{username.lower()}"
    cached = _cache.get(cache_key, ttl=_TTL_STATS)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(base_url=LICHESS_BASE, timeout=15) as client:
        r = await client.get(
            f"/api/user/{username}/rating-history", headers=_headers(ndjson=False)
        )
        r.raise_for_status()

    results = []
    for variant in r.json():
        points = [
            {"date": f"{y}-{m+1:02d}-{d:02d}", "rating": rating}
            for y, m, d, rating in variant.get("points", [])
        ]
        if points:
            results.append({"variant": variant["name"], "history": points})
    _cache.set(cache_key, results)
    return results


@mcp.tool()
async def get_opening_stats(
    username: str,
    color: str = "white",
    speeds: str = "blitz,rapid,classical",
    moves: str = "",
) -> dict:
    """Query the Lichess opening explorer for a player's win/draw/loss stats by opening.

    Call with moves="" to get top-level opening stats, then drill down by passing
    the UCI move sequence (e.g. "e2e4,e7e5") to explore specific variations.

    Args:
        username: Lichess username.
        color: Perspective — "white" or "black".
        speeds: Comma-separated time controls: bullet, blitz, rapid, classical.
        moves: UCI move sequence from the starting position (comma-separated). Empty = start.
    """
    params: dict = {
        "player": username,
        "color": color,
        "speeds": speeds,
        "modes": "rated",
        "recentGames": 0,
    }
    if moves:
        params["play"] = moves

    cache_key = f"opening_stats:{username.lower()}:{color}:{speeds}:{moves}"
    cached = _cache.get(cache_key, ttl=_TTL_STATS)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(base_url=EXPLORER_BASE, timeout=20) as client:
        r = await client.get("/player", params=params)
        r.raise_for_status()

    data = r.json()
    total = data["white"] + data["draws"] + data["black"]
    white_pct = round(100 * data["white"] / total, 1) if total else 0
    draw_pct = round(100 * data["draws"] / total, 1) if total else 0
    black_pct = round(100 * data["black"] / total, 1) if total else 0

    top_moves = []
    for m in data.get("moves", []):
        mt = m["white"] + m["draws"] + m["black"]
        top_moves.append({
            "move": m["san"],
            "uci": m["uci"],
            "games": mt,
            "white_pct": round(100 * m["white"] / mt, 1) if mt else 0,
            "draw_pct": round(100 * m["draws"] / mt, 1) if mt else 0,
            "black_pct": round(100 * m["black"] / mt, 1) if mt else 0,
        })

    result = {
        "total_games": total,
        "results": {"white_pct": white_pct, "draw_pct": draw_pct, "black_pct": black_pct},
        "top_moves": top_moves,
    }
    _cache.set(cache_key, result)
    return result


@mcp.tool()
async def get_opening_performance(
    username: str,
    color: str | None = None,
    max_games: int = 200,
    perf_type: str | None = None,
    min_games: int = 3,
) -> list[dict]:
    """Analyse opening performance from recent games — win/draw/loss rates per opening.

    Shows which openings the player reaches (and which openings opponents play against
    them), broken down by ECO code. Set color="white" to see openings you play as White,
    color="black" to see what opponents play against you as Black.

    Args:
        username: Lichess username.
        color: "white", "black", or None for both.
        max_games: How many recent games to pull (up to 300).
        perf_type: Filter by time control: bullet, blitz, rapid, classical.
        min_games: Minimum games in an opening to include in results.
    """
    params: dict = {
        "max": min(max_games, 300),
        "opening": "true",
        "moves": "false",
        "evals": "false",
        "clocks": "false",
        "rated": "true",
    }
    if color:
        params["color"] = color
    if perf_type:
        params["perfType"] = perf_type

    params_hash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
    cache_key = f"opening_perf:{username.lower()}:{params_hash}"
    cached = _cache.get(cache_key, ttl=_TTL_LIST)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(base_url=LICHESS_BASE, timeout=30) as client:
        r = await client.get(
            f"/api/games/user/{username}", headers=_headers(), params=params
        )
        r.raise_for_status()

    games = _parse_ndjson(r.text)

    # Aggregate W/D/L per opening ECO
    openings: dict[str, dict] = {}
    uid = username.lower()

    for g in games:
        opening = g.get("opening")
        if not opening:
            continue
        eco = opening.get("eco", "?")
        name = opening.get("name", "Unknown")
        key = eco

        players = g.get("players", {})
        white_id = players.get("white", {}).get("user", {}).get("id", "").lower()
        played_as = "white" if white_id == uid else "black"

        winner = g.get("winner")  # "white", "black", or absent (draw/other)
        status = g.get("status", "")
        if winner == played_as:
            outcome = "win"
        elif winner is None or status in ("draw", "stalemate"):
            outcome = "draw"
        else:
            outcome = "loss"

        if key not in openings:
            openings[key] = {
                "eco": eco,
                "name": name,
                "color": played_as,
                "win": 0,
                "draw": 0,
                "loss": 0,
            }
        openings[key][outcome] += 1

    results = []
    for entry in openings.values():
        total = entry["win"] + entry["draw"] + entry["loss"]
        if total < min_games:
            continue
        results.append({
            **entry,
            "games": total,
            "win_pct": round(100 * entry["win"] / total, 1),
            "draw_pct": round(100 * entry["draw"] / total, 1),
            "loss_pct": round(100 * entry["loss"] / total, 1),
        })

    results.sort(key=lambda x: x["games"], reverse=True)
    _cache.set(cache_key, results)
    return results


@mcp.tool()
async def get_puzzle_activity(max_puzzles: int = 50) -> list[dict]:
    """Fetch recent puzzle attempts (requires puzzle:read scope on your token).

    Args:
        max_puzzles: Number of recent puzzle results to fetch.
    """
    cache_key = f"puzzles:{max_puzzles}"
    cached = _cache.get(cache_key, ttl=_TTL_STATS)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(base_url=LICHESS_BASE, timeout=20) as client:
        r = await client.get(
            "/api/puzzle/activity",
            headers=_headers(),
            params={"max": max_puzzles},
        )
        r.raise_for_status()

    result = _parse_ndjson(r.text)
    _cache.set(cache_key, result)
    return result


def _analyse_game(pgn: str, username: str | None = None, depth: int = 18) -> dict:
    """Core Stockfish analysis — called by both analyse_game and fetch_and_analyse_game."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError("Could not parse PGN")

    sf = _sf()
    sf.update_engine_parameters({"Threads": 2, "Hash": 64})
    sf.set_depth(depth)

    headers = dict(game.headers)
    board = game.board()

    counts = {
        "white": {"blunder": 0, "mistake": 0, "inaccuracy": 0, "good": 0, "total_cp_loss": 0},
        "black": {"blunder": 0, "mistake": 0, "inaccuracy": 0, "good": 0, "total_cp_loss": 0},
    }
    moves_out = []

    for node in game.mainline():
        move = node.move
        white_to_move = board.turn == chess.WHITE
        side = "white" if white_to_move else "black"

        sf.set_fen_position(board.fen())
        eval_before = _to_white_cp(sf.get_evaluation(), white_to_move)
        best_uci = sf.get_best_move()

        board.push(move)

        sf.set_fen_position(board.fen())
        eval_after = _to_white_cp(sf.get_evaluation(), not white_to_move)

        cp_loss = max(0, (eval_before - eval_after) if white_to_move else (eval_after - eval_before))
        classification = _classify(cp_loss)

        counts[side][classification] += 1
        counts[side]["total_cp_loss"] += cp_loss

        entry: dict = {
            "ply": node.ply(),
            "move": node.san(),
            "color": side,
            "eval": eval_after,
            "cp_loss": cp_loss,
            "classification": classification,
        }
        if classification in ("blunder", "mistake"):
            entry["best_move"] = best_uci

        moves_out.append(entry)

    def accuracy(c: dict) -> float:
        total = c["blunder"] + c["mistake"] + c["inaccuracy"] + c["good"]
        if not total:
            return 0.0
        penalty = c["blunder"] * 3 + c["mistake"] * 2 + c["inaccuracy"]
        return round(100 * max(0, 1 - penalty / (total * 3)), 1)

    summary = {
        side: {**counts[side], "accuracy": accuracy(counts[side])}
        for side in ("white", "black")
    }

    if username:
        white_name = headers.get("White", "").lower()
        black_name = headers.get("Black", "").lower()
        if username.lower() == white_name:
            summary["you"] = summary["white"]
        elif username.lower() == black_name:
            summary["you"] = summary["black"]

    site = headers.get("Site", "")
    url = site if site.startswith("https://lichess.org/") else None

    return {
        "game": {
            "white": headers.get("White"),
            "black": headers.get("Black"),
            "result": headers.get("Result"),
            "date": headers.get("Date"),
            "opening": headers.get("Opening"),
            "time_control": headers.get("TimeControl"),
            "url": url,
        },
        "summary": summary,
        "moves": moves_out,
    }


@mcp.tool()
def analyse_game(
    pgn: str,
    username: str | None = None,
    depth: int = 18,
) -> dict:
    """Analyse a chess game with local Stockfish.

    Returns a move-by-move breakdown with centipawn evaluations and a
    blunder/mistake/inaccuracy classification for each move, plus a summary
    of each player's accuracy.

    Stockfish must be installed on the system (`brew install stockfish` on Mac).

    Args:
        pgn: Full PGN string of the game.
        username: If provided, highlight which side this player was on.
        depth: Stockfish search depth (default 18; higher = slower but more accurate).
    """
    return _analyse_game(pgn, username=username, depth=depth)


@mcp.tool()
async def fetch_and_analyse_game(
    game_id: str,
    username: str | None = None,
    depth: int = 18,
) -> dict:
    """Fetch a Lichess game by ID and analyse it with local Stockfish.

    Combines game retrieval and move-by-move analysis in one call. Returns the
    same structure as analyse_game: per-move evals, blunder/mistake/inaccuracy
    classifications, and an accuracy summary for each player.

    Stockfish must be installed (`brew install stockfish` on Mac).

    Args:
        game_id: Lichess game ID (the 8-character code in the URL).
        username: If provided, adds a "you" key to the summary for quick lookup.
        depth: Stockfish search depth (default 18).
    """
    analysis_key = f"analysis:{game_id}:{depth}"
    cached = _cache.get(analysis_key)  # permanent — analysis is deterministic
    if cached is not None:
        # Re-attach the "you" summary key without re-running Stockfish
        if username:
            white_name = (cached.get("game") or {}).get("white", "").lower()
            black_name = (cached.get("game") or {}).get("black", "").lower()
            if username.lower() == white_name:
                cached["summary"]["you"] = cached["summary"]["white"]
            elif username.lower() == black_name:
                cached["summary"]["you"] = cached["summary"]["black"]
        return cached

    pgn_key = f"pgn:{game_id}"
    pgn = _cache.get(pgn_key)  # permanent — games don't change
    if pgn is None:
        pgn_headers = {"Accept": "application/x-chess-pgn"}
        if LICHESS_TOKEN:
            pgn_headers["Authorization"] = f"Bearer {LICHESS_TOKEN}"
        async with httpx.AsyncClient(base_url=LICHESS_BASE, timeout=30) as client:
            r = await client.get(
                f"/game/export/{game_id}",
                headers=pgn_headers,
                params={"clocks": "true", "opening": "true", "literate": "false"},
            )
            r.raise_for_status()
        pgn = r.text
        _cache.set(pgn_key, pgn)

    result = _analyse_game(pgn, username=username, depth=depth)

    # Guarantee url is set from game_id even if PGN Site header is absent
    if not result["game"].get("url"):
        result["game"]["url"] = f"{LICHESS_BASE}/{game_id}"

    # Store without the ephemeral "you" key so the cache is username-agnostic
    cacheable = {k: v for k, v in result.items() if k != "summary"}
    summary_without_you = {k: v for k, v in result["summary"].items() if k != "you"}
    cacheable["summary"] = summary_without_you
    _cache.set(analysis_key, cacheable)

    return result


@mcp.tool()
def open_game(game_id: str) -> str:
    """Open a Lichess game in the default browser.

    Args:
        game_id: Lichess game ID (the 8-character code, e.g. "LZmNJsfK").
    """
    url = f"{LICHESS_BASE}/{game_id}"
    subprocess.run(["open", url], check=True)
    return f"Opened {url}"


if __name__ == "__main__":
    mcp.run()
