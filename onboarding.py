"""
onboarding.py — MetaPlay by Coinplay

3-step conversational onboarding. Mateo asks questions, AI extracts
structured preferences from any answer in any language.

Step 1: Sport preference  (football / esports / both)
Step 2: Leagues / games   (specific leagues or esports titles)
Step 3: Teams + style     (favourite teams, value vs picks vs live)

After step 3 → save preferences → show bridge to Coinplay.

Preferences are used to:
  - Filter daily picks and broadcasts
  - Personalize AI responses
  - Personalize Mini App (later)
"""
import json, logging
import httpx
from config import ANTHROPIC_KEY, AI_MODEL
from storage import get_user, update_user, update_preferences, append_history

logger = logging.getLogger(__name__)
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ── Onboarding questions (shown to user) ──────────────────────────────────────

QUESTIONS = {
    "en": [
        ("⚽🎮 *Football or Esports — what's your main thing?*\n\n"
         "Or both? Just say it in your own words."),
        ("Got it. *Which leagues or games specifically?*\n\n"
         "La Liga, Libertadores, Premier League... or CS2, LoL, Dota — whatever you actually follow."),
        ("Perfect. *Any favourite teams or clubs?*\n\n"
         "And what's more useful — breakdowns with full reasoning, live score alerts, or both?"),
    ],
    "ru": [
        ("⚽🎮 *Футбол или киберспорт — что основное?*\n\n"
         "Или и то и другое? Напиши своими словами."),
        ("Понял. *Какие лиги или дисциплины конкретно?*\n\n"
         "Ла Лига, Либертадорес, АПЛ... или CS2, LoL, Dota — то, за чем реально следишь."),
        ("Отлично. *Любимые команды или клубы?*\n\n"
         "И что полезнее — разборы с полной логикой, оповещения о счёте или и то и другое?"),
    ],
    "es": [
        ("⚽🎮 *¿Fútbol o Esports — cuál es tu fuerte?*\n\n"
         "¿O los dos? Contame como quieras."),
        ("Perfecto. *¿Qué ligas o juegos específicamente?*\n\n"
         "La Liga, Libertadores, Premier League... o CS2, LoL, Dota — lo que realmente seguís."),
        ("Bueno. *¿Tenés equipos o clubes favoritos?*\n\n"
         "¿Y qué te sirve más — análisis con razonamiento completo, alertas de resultados, o los dos?"),
    ],
}

DONE_MSG = {
    "en": ("🎯 *Sorted — profile's set up.*\n\n"
           "I'll filter my daily reads and alerts to what you actually follow. "
           "No noise, just the signals that matter.\n\n"
           "Now — where the full breakdowns live 👇"),
    "ru": ("🎯 *Готово — профиль настроен.*\n\n"
           "Буду фильтровать ежедневные разборы и оповещения под то, за чем ты следишь. "
           "Без шума — только нужные сигналы.\n\n"
           "А теперь — где выходят полные разборы 👇"),
    "es": ("🎯 *Listo — perfil configurado.*\n\n"
           "Voy a filtrar mis lecturas diarias y alertas a lo que realmente seguís. "
           "Sin ruido, solo las señales que importan.\n\n"
           "Ahora — dónde viven los análisis completos 👇"),
}


# ── AI preference extractor ───────────────────────────────────────────────────

async def extract_preferences(user_message: str, step: int, lang: str) -> dict:
    """
    Given user's answer at onboarding step N,
    extract structured preferences as JSON.
    Returns partial dict — only fields that were mentioned.
    """
    step_context = {
        0: "User answered about football vs esports preference.",
        1: "User answered about football vs esports preference.",
        2: "User answered about specific leagues (football) or games (esports) they follow.",
        3: "User answered about favourite teams/clubs and preferred signal style.",
    }.get(step, "")

    prompt = f"""Extract sports preferences from this user message.
Context: {step_context}
Message: "{user_message}"

Return ONLY valid JSON with any of these fields that are mentioned:
{{
  "sport": "football" | "esports" | "both" | null,
  "leagues": ["league names as strings"],
  "games": ["cs2" | "lol" | "dota2" | "valorant" | "r6" | "ow2"],
  "teams": ["team or club names"],
  "style": "value" | "picks" | "live" | "all" | null
}}

Rules:
- Only include fields actually mentioned in the message
- Normalize league names: "libertadores" → "Copa Libertadores", "epl" → "Premier League", etc.
- Normalize game names to slugs: "counter strike" → "cs2", "league of legends" → "lol"
- If user says "both" for sport, set sport: "both"
- If user mentions value betting / odds / edge → style: "value"
- If user wants live scores / results → style: "live"  
- If user wants tips / predictions / picks → style: "picks"
- If user wants everything → style: "all"
- Return empty object {{}} if nothing extractable
- NO markdown, NO explanation, ONLY the JSON object"""

    headers = {
        "x-api-key":         ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      AI_MODEL,
        "max_tokens": 200,
        "messages":   [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
    except Exception as e:
        logger.error(f"Preference extraction error: {e}")
        return {}


# ── Onboarding step runner ────────────────────────────────────────────────────

async def process_onboarding_answer(
    user_id: int,
    user_message: str,
    step: int,
    lang: str,
) -> tuple[str | None, bool]:
    """
    Process user's answer for current onboarding step.
    Extracts preferences, saves them.
    Returns (next_question_text | None, onboarding_complete).
    next_question_text = None means onboarding is complete → show bridge.
    """
    # Extract preferences from this answer
    prefs = await extract_preferences(user_message, step, lang)
    if prefs:
        # Merge lists, don't overwrite
        u = get_user(user_id)
        existing = u.get("preferences", {})
        merged = {}
        for field in ("sport", "style"):
            merged[field] = prefs.get(field) or existing.get(field)
        for field in ("leagues", "games", "teams"):
            old = existing.get(field) or []
            new = prefs.get(field) or []
            merged[field] = list(dict.fromkeys(old + new))  # deduplicate, preserve order
        update_preferences(user_id, **merged)
        logger.info(f"User {user_id} prefs updated step={step}: {merged}")

    next_step = step + 1
    questions  = QUESTIONS.get(lang, QUESTIONS["en"])

    if next_step < len(questions):
        return questions[next_step], False
    else:
        return None, True  # onboarding complete


def get_first_question(lang: str) -> str:
    """First onboarding question (step 0 → shown after /start hook)."""
    return QUESTIONS.get(lang, QUESTIONS["en"])[0]


def format_preferences_summary(prefs: dict, lang: str) -> str:
    """Human-readable summary of saved preferences."""
    parts = []
    if prefs.get("sport"):
        parts.append(f"{'Sport' if lang == 'en' else 'Deporte'}: *{prefs['sport']}*")
    if prefs.get("leagues"):
        label = "Leagues" if lang == "en" else "Ligas"
        parts.append(f"{label}: *{', '.join(prefs['leagues'])}*")
    if prefs.get("games"):
        label = "Games" if lang == "en" else "Juegos"
        parts.append(f"{label}: *{', '.join(g.upper() for g in prefs['games'])}*")
    if prefs.get("teams"):
        label = "Teams" if lang == "en" else "Equipos"
        parts.append(f"{label}: *{', '.join(prefs['teams'])}*")
    if prefs.get("style"):
        label = "Style" if lang == "en" else "Estilo"
        parts.append(f"{label}: *{prefs['style']}*")

    if not parts:
        return ""
    header = "📋 *Your profile:*\n" if lang == "en" else "📋 *Tu perfil:*\n"
    return header + "\n".join(f"• {p}" for p in parts)
