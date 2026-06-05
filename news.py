"""
news.py — MetaPlay live content feed for the Mini App.

Aggregates FREE, no-key public feeds into a single normalized list so the
Mini App can fill its "Feed" surface with auto-updating crypto / casino /
esports content. No API keys here on purpose — nothing to leak, nothing to
rate-limit into the ground.

Sources (all free, no auth):
  Crypto   RSS  — CoinDesk, Cointelegraph, Decrypt
  Casino   RSS  — GamblingNews.com, Casino.org
  Esports  JSON — VLR.gg results mirror (vlr.orlandomm.net)
  Market   JSON — CoinGecko /global  +  alternative.me Fear & Greed

Public surface:
  await get_news(category="all", limit=40)  -> {"items":[...], "market":{...}, "updated_at": iso}

Everything is cached in-memory with a TTL and fails soft: a dead feed is
skipped, never fatal.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
NEWS_TTL   = 600   # 10 min — news doesn't move that fast
MARKET_TTL = 180   # 3 min  — prices do
HTTP_TIMEOUT = 12.0
MAX_PER_FEED = 12  # cap per source so one chatty feed can't dominate

# category -> list of (source_label, url)
RSS_FEEDS: dict[str, list[tuple[str, str]]] = {
    "crypto": [
        ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt",       "https://decrypt.co/feed"),
    ],
    "casino": [
        ("GamblingNews",  "https://www.gamblingnews.com/feed/"),
        ("Casino.org",    "https://www.casino.org/news/feed/"),
    ],
}

VLR_RESULTS_URL = "https://vlr.orlandomm.net/api/v1/results"
CG_GLOBAL_URL   = "https://api.coingecko.com/api/v3/global"
CG_MARKETS_URL  = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&ids=bitcoin,ethereum,solana&sparkline=false"
)
FNG_URL         = "https://api.alternative.me/fng/?limit=1"

_UA = "MetaPlayArena/1.0 (+https://t.me)"

# ── Cache ───────────────────────────────────────────────────────────────────--
_cache: dict[str, dict] = {}


def _cached(key: str):
    e = _cache.get(key)
    if e and time.time() - e["ts"] < e["ttl"]:
        return e["data"]
    return None


def _set_cache(key: str, data, ttl: int):
    _cache[key] = {"ts": time.time(), "data": data, "ttl": ttl}


# ── Helpers ────────────────────────────────────────────────────────────────---
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE  = re.compile(r"\s+")
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def _strip_html(s: str | None, limit: int = 220) -> str:
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def _to_iso(date_str: str | None) -> str:
    """pubDate (RFC-822, RSS) or updated/published (ISO-8601, Atom) → ISO-8601 UTC.

    Falls back to 'now' on garbage so the item still sorts sanely.
    """
    if date_str:
        s = date_str.strip()
        # ISO-8601 (Atom): 2025-06-04T10:00:00Z / +00:00
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
        # RFC-822 (RSS): Wed, 04 Jun 2025 14:30:00 GMT
        try:
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _first_image(item: ET.Element, description: str) -> str | None:
    # 1) <media:content url> / <media:thumbnail url>
    for tag in ("{http://search.yahoo.com/mrss/}content",
                "{http://search.yahoo.com/mrss/}thumbnail"):
        el = item.find(tag)
        if el is not None and el.get("url"):
            return el.get("url")
    # 2) <enclosure url type="image/...">
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image") and enc.get("url"):
        return enc.get("url")
    # 3) first <img> inside the description / content:encoded
    m = _IMG_RE.search(description or "")
    if m:
        return m.group(1)
    return None


def _stable_id(url: str) -> str:
    return str(abs(hash(url)) % (10 ** 12))


def _parse_rss(xml_text: str, source: str, category: str) -> list[dict]:
    """Parse an RSS/Atom string into normalized news items. Never raises."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("RSS parse error (%s): %s", source, e)
        return items

    # RSS 2.0: channel/item   |   Atom: feed/entry
    nodes = root.findall(".//item")
    is_atom = False
    if not nodes:
        nodes = root.findall("{http://www.w3.org/2005/Atom}entry")
        is_atom = True

    for n in nodes[:MAX_PER_FEED]:
        if is_atom:
            title = (n.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = n.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href") if link_el is not None else "") or ""
            desc = (n.findtext("{http://www.w3.org/2005/Atom}summary")
                    or n.findtext("{http://www.w3.org/2005/Atom}content") or "")
            pub = (n.findtext("{http://www.w3.org/2005/Atom}updated")
                   or n.findtext("{http://www.w3.org/2005/Atom}published"))
        else:
            title = (n.findtext("title") or "").strip()
            link = (n.findtext("link") or "").strip()
            desc = (n.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                    or n.findtext("description") or "")
            pub = n.findtext("pubDate")

        if not title or not link:
            continue

        items.append({
            "id":           _stable_id(link),
            "title":        html.unescape(title),
            "url":          link,
            "source":       source,
            "category":     category,
            "published_at": _to_iso(pub if not is_atom else pub),
            "image":        _first_image(n, desc),
            "summary":      _strip_html(desc),
        })
    return items


# ── Fetchers ───────────────────────────────────────────────────────────────---

async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, headers={"User-Agent": _UA, "Accept": "*/*"})
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001 — feeds fail soft
        logger.warning("feed fetch failed %s: %s", url, type(e).__name__)
        return None


async def _fetch_json(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("json fetch failed %s: %s", url, type(e).__name__)
        return None


async def _fetch_rss_category(client, category: str) -> list[dict]:
    feeds = RSS_FEEDS.get(category, [])
    texts = await asyncio.gather(*[_fetch_text(client, u) for _, u in feeds])
    out: list[dict] = []
    for (source, _), text in zip(feeds, texts):
        if text:
            out.extend(_parse_rss(text, source, category))
    return out


async def _fetch_esports(client) -> list[dict]:
    data = await _fetch_json(client, VLR_RESULTS_URL)
    out: list[dict] = []
    if not data:
        return out
    results = (data.get("data", {}) or {}).get("segments") or data.get("data") or []
    if isinstance(results, dict):
        results = results.get("segments", [])
    for r in (results or [])[:MAX_PER_FEED]:
        try:
            t1, t2 = r.get("team1", "?"), r.get("team2", "?")
            s1, s2 = r.get("score1", ""), r.get("score2", "")
            event  = r.get("tournament") or r.get("event") or "Valorant"
            title  = f"{t1} {s1}–{s2} {t2}"
            link   = "https://www.vlr.gg" + (r.get("match_page", "") or "")
            out.append({
                "id":           _stable_id(link + title),
                "title":        title,
                "url":          link,
                "source":       "VLR.gg",
                "category":     "esports",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "image":        None,
                "summary":      f"{event} · {r.get('round_info', 'Result')}",
            })
        except Exception:  # noqa: BLE001
            continue
    return out


async def _fetch_market(client) -> dict:
    cached = _cached("market")
    if cached is not None:
        return cached

    g, coins, fng = await asyncio.gather(
        _fetch_json(client, CG_GLOBAL_URL),
        _fetch_json(client, CG_MARKETS_URL),
        _fetch_json(client, FNG_URL),
    )

    market: dict = {"coins": [], "fng": None, "mcap_change_24h": None, "btc_dominance": None}

    if isinstance(g, dict):
        d = g.get("data", {}) or {}
        market["mcap_change_24h"] = d.get("market_cap_change_percentage_24h_usd")
        market["btc_dominance"] = (d.get("market_cap_percentage", {}) or {}).get("btc")

    if isinstance(coins, list):
        for c in coins:
            market["coins"].append({
                "symbol": (c.get("symbol") or "").upper(),
                "name":   c.get("name"),
                "price":  c.get("current_price"),
                "change_24h": c.get("price_change_percentage_24h"),
                "image":  c.get("image"),
            })

    if isinstance(fng, dict):
        arr = fng.get("data") or []
        if arr:
            f0 = arr[0]
            market["fng"] = {
                "value": int(f0.get("value", 0)),
                "label": f0.get("value_classification", ""),
            }

    _set_cache("market", market, MARKET_TTL)
    return market


# ── Public API ────────────────────────────────────────────────────────────────

VALID_CATEGORIES = ("all", "crypto", "casino", "esports")


async def get_news(category: str = "all", limit: int = 40) -> dict:
    """Return {items, market, updated_at}. Cached; fails soft on dead feeds."""
    if category not in VALID_CATEGORIES:
        category = "all"

    cache_key = f"news:{category}:{limit}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        if category == "all":
            crypto, casino, esports, market = await asyncio.gather(
                _fetch_rss_category(client, "crypto"),
                _fetch_rss_category(client, "casino"),
                _fetch_esports(client),
                _fetch_market(client),
            )
            items = crypto + casino + esports
        elif category == "esports":
            esports, market = await asyncio.gather(
                _fetch_esports(client),
                _fetch_market(client),
            )
            items = esports
        else:
            items, market = await asyncio.gather(
                _fetch_rss_category(client, category),
                _fetch_market(client),
            )

    # newest first, dedupe by url, cap
    seen: set[str] = set()
    deduped: list[dict] = []
    for it in sorted(items, key=lambda x: x["published_at"], reverse=True):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        deduped.append(it)
        if len(deduped) >= limit:
            break

    payload = {
        "items":      deduped,
        "market":     market,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # short TTL if everything failed, so we retry soon instead of caching emptiness
    _set_cache(cache_key, payload, NEWS_TTL if deduped else 60)
    return payload
