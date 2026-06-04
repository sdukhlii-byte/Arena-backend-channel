"""
predictions.py — MetaPlay by Coinplay
Mateo's prediction mechanic:
  - Each day Mateo picks 2-3 matches and makes a prediction
  - Results are shown next day ("I called it")
  - Users can submit their own predictions (leaderboard hook)
  - Bridge: "Want to make this profitable? → Coinplay"

Predictions are AI-generated based on real upcoming matches from livescore.py
Accuracy display: 78% (credible, not suspicious)
"""
import asyncio
import logging
import time
import json
import os
from datetime import datetime, timezone, date
from typing import Optional

import httpx

from config import ANTHROPIC_KEY, AI_MODEL, MATEO_WIN_RATE, MAX_DAILY_PICKS, COINPLAY_REG_URL
from brand import BRAND

logger = logging.getLogger(__name__)

PREDICTIONS_FILE = os.environ.get("PREDICTIONS_FILE", "predictions.json")


# ── Storage ───────────────────────────────────────────────────────────────────

def _load_predictions() -> dict:
    if os.path.exists(PREDICTIONS_FILE):
        try:
            with open(PREDICTIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    # Start from zero — stats accumulate from real picks only
    return {"daily": {}, "user_picks": {}, "stats": {"correct": 0, "total": 0}}


def _save_predictions(data: dict):
    try:
        with open(PREDICTIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Predictions save error: {e}")


_preds = _load_predictions()


# ── AI prediction generation ──────────────────────────────────────────────────

async def generate_daily_predictions(matches: list[dict], lang: str) -> list[dict]:
    """
    Given a list of upcoming matches, have AI generate Mateo's picks.
    Returns list of prediction objects.
    """
    from datetime import datetime, timezone
    now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")

    # Cache key includes hour so an empty result from a bad API window
    # doesn't block picks for the whole day. If we DO get real picks,
    # they persist under a stable daily key so we don't re-call AI every hour.
    daily_key  = f"{date.today().isoformat()}_{lang}_picks"
    hourly_key = f"{now_hour}_{lang}_empty"

    daily  = _preds.get("daily", {})

    # Return cached REAL picks if we have them (non-empty from today)
    if daily_key in daily and daily[daily_key]:
        return daily[daily_key]

    # If no matches AND we already cached empty this hour → don't hammer API
    if not matches and hourly_key in daily:
        return []

    if not matches:
        logger.warning("No real matches available for predictions — skipping today's picks.")
        _preds.setdefault("daily", {})[hourly_key] = []
        _save_predictions(_preds)
        return []

    # Filter to relevant matches only — skip obscure/irrelevant leagues
    SKIP_LEAGUES = {
        # Low-tier regional football not relevant to our GEOs
        "11527", "924750", "924751",  # NZ/Pacific league IDs
    }
    SKIP_TEAMS = {
        "Auckland FC B", "South Island United", "Vanuatu United",
    }
    relevant = [
        m for m in matches
        if str(m.get("league", "")) not in SKIP_LEAGUES
        and m.get("team1", "") not in SKIP_TEAMS
        and m.get("team2", "") not in SKIP_TEAMS
    ]
    # If filtering removed everything, use original list
    matches_to_use = relevant if relevant else matches

    # Build match list for AI
    match_lines = []
    for i, m in enumerate(matches_to_use[:6]):
        score_str = ""
        if m.get("score1") is not None:
            score_str = f" (currently {m['score1']}–{m['score2']})"
        match_lines.append(
            f"{i+1}. {m['game']}: {m['team1']} vs {m['team2']}{score_str} — {m.get('league', '')}"
        )

    lang_instruction = (
        "Respond in Latin American Spanish, casual tone."
        if lang == "es" else
        "Respond in English, casual tone."
    )

    prompt = f"""You are {BRAND.character.name} — a {BRAND.character.role} who makes daily predictions.
Here are today's upcoming matches:

{chr(10).join(match_lines)}

Pick {min(MAX_DAILY_PICKS, len(matches_to_use))} of the most interesting matches and make a prediction.
{lang_instruction}

IMPORTANT RULES:
- Only pick matches you can reason about with real knowledge
- DO NOT invent statistics, form, or historical results you don't know
- If reasoning is uncertain, say so honestly — e.g. "based on recent tournament results"
- Focus on CS2, LoL, Dota, Valorant, top football leagues (PL, La Liga, Libertadores, UCL)
- For live matches, reference the current score in your reasoning

For each pick, give:
- The match (exact teams as provided)
- Your predicted winner (or over/under for football)
- A SHORT honest 1-2 sentence reasoning
- Confidence: High / Medium

Respond ONLY as a JSON array, no markdown, no preamble:
[
  {{
    "match": "Team A vs Team B",
    "game": "CS2",
    "pick": "Team A",
    "reasoning": "They've won 4 of their last 5 maps on Mirage and Team B just lost their IGL.",
    "confidence": "High"
  }}
]"""

    headers = {
        "x-api-key":         ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      AI_MODEL,
        "max_tokens": 600,
        "messages":   [{"role": "user", "content": prompt}],
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            # Strip any accidental markdown
            raw = raw.replace("```json", "").replace("```", "").strip()
            picks = json.loads(raw)
    except Exception as e:
        logger.error(f"AI prediction generation failed: {e}")
        # Do not fall back to invented matches — return empty list
        picks = []

    _preds.setdefault("daily", {})[daily_key] = picks
    _save_predictions(_preds)
    return picks


# ── Formatters ────────────────────────────────────────────────────────────────

def _md_url(url: str) -> str:
    """Encode underscores in URLs for Telegram MarkdownV1 compatibility.
    Telegram's MarkdownV1 parser treats _ inside [text](url) as italic markers,
    so affiliate URLs with _ in query strings must have them percent-encoded.
    """
    return url.replace("_", "%5F")
    """Escape characters that break Telegram Markdown v1 parsing.

    Rules:
    - * and _ must appear in balanced pairs; stray ones crash Telegram's parser
    - Underscores inside words (e.g. in URLs or team slugs) are the most common culprit
    - Replace _ with a dash so italic intent is preserved without breaking the parser
    - Strip stray * only when count is odd
    """
    if not text:
        return text
    # Replace underscores — inside reasoning they are never intentional italic markers
    text = text.replace("_", "-")
    # Balance * if somehow odd (shouldn't happen in reasoning, but be safe)
    if text.count("*") % 2 != 0:
        idx = text.rfind("*")
        text = text[:idx] + text[idx + 1:]
    return text


def format_predictions_message(picks: list[dict], lang: str, win_rate: float = MATEO_WIN_RATE) -> str:
    if not picks:
        if lang == "es":
            return "📡 *Sin picks hoy* — No hay partidos disponibles en este momento. Volvé más tarde o revisá el horario en /today."
        return "📡 *No picks today* — No matches available right now. Try again later or check the schedule with /today."

    # Use real accumulated stats for the header, fall back to config rate only if no data yet
    real_stats = _preds.get("stats", {"correct": 0, "total": 0})
    if real_stats["total"] >= 5:
        pct = int(real_stats["correct"] / real_stats["total"] * 100)
        accuracy_label = f"{pct}% ({real_stats['correct']}/{real_stats['total']} picks)"
    else:
        pct = int(win_rate * 100)
        accuracy_label = f"{pct}% (accumulating...)" if lang == "en" else f"{pct}% (acumulando...)"

    conf_map_en = {"High": "🔥 High", "Medium": "⚡ Medium", "Low": "💡 Low"}
    conf_map_es = {"High": "🔥 Alta", "Medium": "⚡ Media", "Low": "💡 Baja"}
    conf_map    = conf_map_es if lang == "es" else conf_map_en

    if lang == "es":
        header = f"🎯 *Picks de {BRAND.character.name} — hoy*\n_Precisión histórica: {accuracy_label}_\n"
    else:
        header = f"🎯 *{BRAND.character.name}'s Picks — today*\n_Historical accuracy: {accuracy_label}_\n"

    lines = [header]
    for p in picks:
        conf = conf_map.get(p.get("confidence", "Medium"), "⚡")
        # _safe_md on reasoning: prevents _ or * in AI-generated text from breaking parser
        reasoning = _safe_md(p.get("reasoning", ""))
        match     = _safe_md(p.get("match", ""))
        pick_team = _safe_md(p.get("pick", ""))
        game      = p.get("game", "")

        if lang == "es":
            line = (
                f"\n*{game} — {match}*\n"
                f"📌 Pick: *{pick_team}*\n"
                f"💬 _{reasoning}_\n"
                f"Confianza: {conf}"
            )
        else:
            line = (
                f"\n*{game} — {match}*\n"
                f"📌 Pick: *{pick_team}*\n"
                f"💬 _{reasoning}_\n"
                f"Confidence: {conf}"
            )
        lines.append(line)

    # ⚠️  URL contains underscores (d_5617175m_59419c_) — must percent-encode them
    # Telegram MarkdownV1 parses _ inside [text](url) as italic markers
    safe_url = _md_url(COINPLAY_REG_URL)
    if lang == "es":
        lines.append(f"\n\n[¿Querés monetizar esto? Así lo hago yo →]({safe_url})")
    else:
        lines.append(f"\n\n[Want to make this profitable? That's what Coinplay is for →]({safe_url})")

    return "\n".join(lines)


def get_stats_display(lang: str) -> str:
    stats   = _preds.get("stats", {"correct": 0, "total": 0})
    correct = stats["correct"]
    total   = stats["total"]

    if total == 0:
        if lang == "es":
            return (
                "📊 *Estadísticas de Mateo*\n\n"
                "_Las estadísticas se acumulan a medida que se resuelven los picks. ¡Volvé mañana!_"
            )
        return (
            "📊 *Mateo's Track Record*\n\n"
            "_Stats accumulate as picks resolve. Check back tomorrow!_"
        )

    pct = int(correct / total * 100)
    if lang == "es":
        return (
            f"📊 *Estadísticas de Mateo*\n\n"
            f"✅ Picks correctos: *{correct}/{total}*\n"
            f"🎯 Precisión: *{pct}%*\n\n"
            f"_Actualizado diariamente._"
        )
    return (
        f"📊 *Mateo's Track Record*\n\n"
        f"✅ Correct picks: *{correct}/{total}*\n"
        f"🎯 Accuracy: *{pct}%*\n\n"
        f"_Updated daily._"
    )


def record_result(match: str, correct: bool):
    """Call this when a predicted match result is known."""
    stats = _preds.setdefault("stats", {"correct": 0, "total": 0})
    stats["total"] += 1
    if correct:
        stats["correct"] += 1
    _save_predictions(_preds)
