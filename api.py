"""
api.py — MetaPlay HTTP API for the Mini App

Fully async — runs on asyncio, no threading issues.
Uses only stdlib (no aiohttp dependency needed).

Endpoints:
  GET /api/health
  GET /api/live
  GET /api/upcoming
  GET /api/picks?lang=en
  GET /api/stats
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlparse, parse_qs

from predictions import generate_daily_predictions, _preds
from livescore import fetch_match_context, get_live_esports, get_live_football, get_upcoming_esports, get_today_football

logger = logging.getLogger(__name__)
PORT        = int(os.environ.get("PORT", os.environ.get("API_PORT", 8080)))
CORS_ORIGIN = os.environ.get("MINI_APP_ORIGIN", "*")


def _json_bytes(data) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _cors_headers(body: bytes) -> list[tuple[str, str]]:
    return [
        ("Content-Type",                 "application/json; charset=utf-8"),
        ("Access-Control-Allow-Origin",  CORS_ORIGIN),
        ("Access-Control-Allow-Methods", "GET, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"),
        ("Content-Length",               str(len(body))),
    ]


# ── Route handlers (all async) ────────────────────────────────────────────────

async def handle_health() -> tuple[int, bytes]:
    return 200, _json_bytes({"status": "ok"})


async def handle_stats() -> tuple[int, bytes]:
    stats   = _preds.get("stats", {"correct": 0, "total": 0})
    total   = stats["total"]
    correct = stats["correct"]
    return 200, _json_bytes({
        "correct": correct,
        "total":   total,
        "rate":    round(correct / total * 100) if total > 0 else None,
        "note":    "accumulating" if total < 5 else "real",
    })


async def handle_live() -> tuple[int, bytes]:
    (live_e, _), (live_f, _) = await asyncio.gather(
        get_live_esports(),
        get_live_football(),
    )
    return 200, _json_bytes({"matches": live_e + live_f})


async def handle_upcoming() -> tuple[int, bytes]:
    (up_e, _), (today_f, _) = await asyncio.gather(
        get_upcoming_esports(24),
        get_today_football(),
    )
    return 200, _json_bytes({"matches": up_e + today_f})


async def handle_picks(lang: str) -> tuple[int, bytes]:
    ctx          = await fetch_match_context()
    real_matches = ctx.get("upcoming", [])
    picks        = await generate_daily_predictions(real_matches, lang)
    stats        = _preds.get("stats", {"correct": 0, "total": 0})
    total        = stats["total"]
    correct      = stats["correct"]
    return 200, _json_bytes({
        "picks": picks,
        "stats": {
            "correct": correct,
            "total":   total,
            "rate":    round(correct / total * 100) if total > 0 else None,
            "note":    "accumulating" if total < 5 else "real",
        },
        "source": "real" if real_matches else "no_matches",
    })


# ── Async HTTP server ─────────────────────────────────────────────────────────

async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        raw = await reader.read(4096)
        if not raw:
            writer.close()
            return

        first_line = raw.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts      = first_line.split(" ")
        method     = parts[0] if parts else "GET"
        full_path  = parts[1] if len(parts) > 1 else "/"

        parsed = urlparse(full_path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        # OPTIONS preflight
        if method == "OPTIONS":
            response = (
                b"HTTP/1.1 204 No Content\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Access-Control-Allow-Methods: GET, OPTIONS\r\n"
                b"Access-Control-Allow-Headers: Content-Type\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
            writer.write(response)
            await writer.drain()
            writer.close()
            return

        # Route
        try:
            if path == "/api/health":
                status, body = await handle_health()
            elif path == "/api/stats":
                status, body = await handle_stats()
            elif path == "/api/live":
                status, body = await handle_live()
            elif path == "/api/upcoming":
                status, body = await handle_upcoming()
            elif path == "/api/picks":
                lang = (qs.get("lang", ["en"])[0] or "en").lower()
                if lang not in ("en", "es"):
                    lang = "en"
                status, body = await handle_picks(lang)
            else:
                status, body = 404, _json_bytes({"error": "not_found"})
        except Exception as e:
            logger.error(f"{path} handler error: {e}", exc_info=True)
            status, body = 500, _json_bytes({"error": "internal_error"})

        headers = _cors_headers(body)
        status_text = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        header_lines = "\r\n".join(f"{k}: {v}" for k, v in headers)
        response_head = f"HTTP/1.1 {status} {status_text}\r\n{header_lines}\r\n\r\n".encode()

        writer.write(response_head + body)
        await writer.drain()
        logger.info(f'"{method} {path} HTTP/1.1" {status} -')

    except Exception as e:
        logger.error(f"Request handling error: {e}")
    finally:
        writer.close()


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    server = await asyncio.start_server(handle_request, "0.0.0.0", PORT)
    logger.info(f"MetaPlay Mini App API listening on :{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
